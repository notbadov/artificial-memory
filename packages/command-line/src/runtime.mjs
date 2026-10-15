// The signed client runtime: the standalone Python that runs the command-line client and the
// synchronization client. This is the same artifact, index, signature, directory and pointer
// files the desktop application uses, so the two share one runtime on a computer that has
// both and each finds what the other installed.

import { spawnSync } from "node:child_process";
import { createHash, createPublicKey, verify } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { gunzipSync } from "node:zlib";

export const RUNTIME_INDEX_PATH = "/downloads/client-runtime/latest.json";
const DOMAIN = Buffer.from("agent-platform-client-runtime-v1\0", "latin1");
const MAX_ARCHIVE_BYTES = 256 * 1024 * 1024;
const MAX_INDEX_BYTES = 256 * 1024;
const MAX_UNPACKED_BYTES = 2 * 1024 * 1024 * 1024;
const STALE_STAGING_MS = 24 * 60 * 60 * 1000;
const ED25519_SPKI_PREFIX = Buffer.from("302a300506032b6570032100", "hex");
const MANIFEST_FIELDS = [
  "runtimeId", "platform", "agentPlatformSha", "pythonVersion", "pythonSha256", "archive",
  "archiveSha256", "archiveSize", "executable", "installer", "capabilities", "createdAt",
];

export class RuntimeError extends Error {
  /** `code` is stable; `offline` marks the one failure an installed runtime may outlive. */
  constructor(code, detail = "", offline = false) {
    super(detail ? `${code}: ${detail}` : code);
    this.code = code;
    this.offline = offline;
  }
}

export function runtimeBase(home) {
  return path.join(home, ".agent-platform", "runtime");
}

/** Platform key of the runtime index for this computer, or null when there is no runtime for it. */
export function platformKey(platform = process.platform, arch = process.arch) {
  if (platform === "darwin" && arch === "arm64") return "darwin-aarch64";
  if (platform === "darwin" && arch === "x64") return "darwin-x86_64";
  if (platform === "win32" && arch === "x64") return "windows-x86_64";
  return null;
}

/**
 * JSON exactly as Python's `json.dumps(sort_keys=True, separators=(",", ":"))` writes it for
 * the printable-ASCII manifests the build tool produces. Anything else is refused.
 */
export function canonicalJson(value) {
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) throw new RuntimeError("runtime_manifest_invalid", "non-integer number");
    return String(value);
  }
  if (typeof value === "string") {
    let out = '"';
    for (const character of value) {
      if (character === '"') out += '\\"';
      else if (character === "\\") out += "\\\\";
      else if (character >= " " && character <= "~") out += character;
      else throw new RuntimeError("runtime_manifest_invalid", "non-printable or non-ASCII text");
    }
    return out + '"';
  }
  if (Array.isArray(value)) return "[" + value.map(canonicalJson).join(",") + "]";
  if (typeof value === "object") {
    return "{" + Object.keys(value).sort().map((key) => canonicalJson(key) + ":" + canonicalJson(value[key])).join(",") + "}";
  }
  throw new RuntimeError("runtime_manifest_invalid", "unsupported value");
}

export function signingBytes(envelope) {
  const unsigned = { schemaVersion: envelope.schemaVersion, keyId: envelope.keyId, manifest: envelope.manifest };
  return Buffer.concat([DOMAIN, Buffer.from(canonicalJson(unsigned), "latin1")]);
}

function safeName(value) {
  return typeof value === "string" && /^[A-Za-z0-9_-][A-Za-z0-9._-]{0,199}$/.test(value);
}

function normalComponents(value) {
  if (typeof value !== "string" || value === "" || value.includes("\\") || value.includes("\0")) return null;
  const parts = value.split("/").filter((part, index, all) => !(part === "" && index === all.length - 1));
  return parts.length > 0 && parts.every((part) => part !== "" && part !== "." && part !== "..") ? parts : null;
}

function decodeExact(value, length) {
  if (typeof value !== "string" || !/^[A-Za-z0-9+/]*={0,2}$/.test(value)) return null;
  const bytes = Buffer.from(value, "base64");
  return bytes.length === length ? bytes : null;
}

/** Verifies an envelope against `keys` ({keyId: base64 key}) and returns its manifest for `platform`. */
export function verifyEnvelope(envelope, keys, platform) {
  if (envelope === null || typeof envelope !== "object" || Array.isArray(envelope)
      || Object.keys(envelope).sort().join() !== "keyId,manifest,schemaVersion,signature") {
    throw new RuntimeError("runtime_index_invalid", "envelope");
  }
  if (envelope.schemaVersion !== 1) throw new RuntimeError("runtime_manifest_invalid", "schema version");
  const manifest = envelope.manifest;
  const keyBytes = Object.hasOwn(keys, envelope.keyId) ? decodeExact(keys[envelope.keyId], 32) : null;
  const signature = decodeExact(envelope.signature, 64);
  if (!keyBytes || !signature || manifest === null || typeof manifest !== "object" || Array.isArray(manifest)) {
    throw new RuntimeError("runtime_signature_invalid");
  }
  const key = createPublicKey({ key: Buffer.concat([ED25519_SPKI_PREFIX, keyBytes]), format: "der", type: "spki" });
  if (!verify(null, signingBytes(envelope), key, signature)) throw new RuntimeError("runtime_signature_invalid");
  if (Object.keys(manifest).sort().join() !== [...MANIFEST_FIELDS].sort().join()) {
    throw new RuntimeError("runtime_manifest_invalid", "fields");
  }
  if (manifest.platform !== platform) throw new RuntimeError("runtime_manifest_invalid", "platform");
  if (!safeName(manifest.runtimeId) || !safeName(manifest.archive)) throw new RuntimeError("runtime_manifest_invalid", "names");
  if (!normalComponents(manifest.executable) || !normalComponents(manifest.installer)) {
    throw new RuntimeError("runtime_manifest_invalid", "entry points");
  }
  if (typeof manifest.archiveSha256 !== "string" || !/^[0-9a-f]{64}$/.test(manifest.archiveSha256)) {
    throw new RuntimeError("runtime_manifest_invalid", "digest");
  }
  if (!Number.isSafeInteger(manifest.archiveSize) || manifest.archiveSize <= 0 || manifest.archiveSize > MAX_ARCHIVE_BYTES) {
    throw new RuntimeError("runtime_manifest_invalid", "size");
  }
  if (!Array.isArray(manifest.capabilities) || !manifest.capabilities.every((item) => typeof item === "string")) {
    throw new RuntimeError("runtime_manifest_invalid", "capabilities");
  }
  if (createdTime(manifest.createdAt) === null) throw new RuntimeError("runtime_manifest_invalid", "creation time");
  return manifest;
}

function createdTime(value) {
  if (typeof value !== "string" || !/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$/.test(value)) return null;
  const time = Date.parse(value);
  return Number.isNaN(time) ? null : time;
}

/**
 * When an installed runtime was built, for telling an older offer from a newer one. The signed
 * manifest carries `createdAt`; a pointer written before that was kept (or by Desktop Companion
 * up to 0.4.3) has only the runtime id, which starts with the day of the build. Null when
 * neither is readable: then nothing is known and nothing is refused.
 */
export function builtAt(pointer) {
  const recorded = createdTime(pointer?.createdAt);
  if (recorded !== null) return recorded;
  const day = /^([0-9]{4})([0-9]{2})([0-9]{2})-/.exec(pointer?.runtimeId ?? "");
  return day ? createdTime(`${day[1]}-${day[2]}-${day[3]}T00:00:00Z`) : null;
}

/** Picks this platform's entry from `latest.json` and returns it with its download path. */
export function selectFromIndex(index, keys, platform) {
  const entry = index?.platforms?.[platform];
  if (!entry || typeof entry !== "object") throw new RuntimeError("runtime_index_invalid", `no runtime for ${platform}`);
  const manifest = verifyEnvelope(entry.envelope, keys, platform);
  // The path comes from the signed manifest, not from the unsigned index.
  return { manifest, path: `/downloads/client-runtime/${manifest.runtimeId}/${manifest.archive}` };
}

export function checkArchive(bytes, manifest) {
  if (bytes.length !== manifest.archiveSize || createHash("sha256").update(bytes).digest("hex") !== manifest.archiveSha256) {
    throw new RuntimeError("runtime_digest_mismatch");
  }
}

function tarText(block, start, length) {
  const end = block.indexOf(0, start);
  return block.toString("utf8", start, end === -1 || end > start + length ? start + length : end);
}

function tarNumber(block, start, length) {
  const text = tarText(block, start, length).trim();
  if (!/^[0-7]*$/.test(text)) throw new RuntimeError("runtime_archive_unsafe", "header number");
  return text === "" ? 0 : parseInt(text, 8);
}

function paxRecords(data) {
  const records = {};
  let offset = 0;
  while (offset < data.length) {
    const space = data.indexOf(0x20, offset);
    const length = space === -1 ? NaN : Number(data.toString("latin1", offset, space));
    if (!Number.isSafeInteger(length) || length <= space - offset + 1 || offset + length > data.length) {
      throw new RuntimeError("runtime_archive_unsafe", "extended header");
    }
    const record = data.toString("utf8", space + 1, offset + length - 1);
    const equals = record.indexOf("=");
    if (equals > 0) records[record.slice(0, equals)] = record.slice(equals + 1);
    offset += length;
  }
  return records;
}

/**
 * Unpacks a verified archive into `destination`, refusing absolute paths, `..`, links that
 * leave the directory and anything that is not a file or directory. Relative symlinks are
 * accepted on Unix; Windows runtime archives must not contain links.
 */
export function extractArchive(bytes, destination, { windows = process.platform === "win32" } = {}) {
  let tar;
  try {
    tar = gunzipSync(bytes, { maxOutputLength: MAX_UNPACKED_BYTES });
  } catch {
    throw new RuntimeError("runtime_archive_unsafe", "not a gzip archive");
  }
  fs.mkdirSync(destination, { recursive: true });
  const realRoot = fs.realpathSync(destination);
  const inside = (real) => real === realRoot || real.startsWith(realRoot + path.sep);
  // The directory an entry goes into, made one component at a time and never through a link
  // that leads out. A lexical check of each link is not enough: d/l1 -> .. stays inside, and
  // d/l2 -> l1/.. then leaves through it.
  const directoryFor = (parts, name) => {
    let current = realRoot;
    for (const part of parts) {
      current = path.join(current, part);
      let state;
      try {
        state = fs.lstatSync(current);
      } catch {
        fs.mkdirSync(current);
        continue;
      }
      if (state.isSymbolicLink()) {
        let real;
        try {
          real = fs.realpathSync(current);
        } catch {
          throw new RuntimeError("runtime_archive_unsafe", `path ${name}`);
        }
        if (!inside(real) || !fs.statSync(real).isDirectory()) throw new RuntimeError("runtime_archive_unsafe", `path ${name}`);
        current = real;
      } else if (!state.isDirectory()) {
        throw new RuntimeError("runtime_archive_unsafe", `path ${name}`);
      }
    }
    return current;
  };
  // Where a link would really lead from `directory`, following the links already unpacked.
  const linkTarget = (directory, link) => {
    let current = directory;
    for (const part of link.split("/")) {
      if (part === "" || part === ".") continue;
      if (part === "..") {
        current = path.dirname(current);
        continue;
      }
      const next = path.join(current, part);
      try {
        current = fs.realpathSync(next);
      } catch {
        current = next;
      }
    }
    return current;
  };
  let offset = 0;
  let extended = {};
  while (offset + 512 <= tar.length) {
    const header = tar.subarray(offset, offset + 512);
    offset += 512;
    if (header.every((byte) => byte === 0)) break;
    const size = tarNumber(header, 124, 12);
    const data = tar.subarray(offset, offset + size);
    if (data.length !== size) throw new RuntimeError("runtime_archive_unsafe", "truncated");
    offset += Math.ceil(size / 512) * 512;
    const kind = String.fromCharCode(header[156] || 0x30);
    if (kind === "x") {
      extended = paxRecords(data);
      continue;
    }
    if (kind === "L" || kind === "K") {
      extended[kind === "L" ? "path" : "linkpath"] = tarText(data, 0, data.length);
      continue;
    }
    const prefix = tarText(header, 345, 155);
    const name = extended.path ?? (prefix ? `${prefix}/${tarText(header, 0, 100)}` : tarText(header, 0, 100));
    const link = extended.linkpath ?? tarText(header, 157, 100);
    extended = {};
    const parts = normalComponents(name);
    if (!parts) throw new RuntimeError("runtime_archive_unsafe", `path ${name}`);
    if (kind === "5") {
      directoryFor(parts, name);
      continue;
    }
    const directory = directoryFor(parts.slice(0, -1), name);
    const target = path.join(directory, parts[parts.length - 1]);
    if (fs.lstatSync(target, { throwIfNoEntry: false })) throw new RuntimeError("runtime_archive_unsafe", `path ${name}`);
    if (kind === "0") {
      fs.writeFileSync(target, data, { flag: "wx" });
      if (!windows) fs.chmodSync(target, (tarNumber(header, 100, 8) || 0o644) & 0o755);
    } else if (kind === "2") {
      if (windows) throw new RuntimeError("runtime_archive_unsafe", "symlinks are not expected in the Windows runtime");
      if (link === "" || link.startsWith("/") || link.includes("\\")) throw new RuntimeError("runtime_archive_unsafe", `link ${name}`);
      let depth = parts.length - 1;
      for (const part of link.split("/")) {
        if (part === "..") depth -= 1;
        else if (part !== "." && part !== "") depth += 1;
        if (depth < 0) throw new RuntimeError("runtime_archive_unsafe", `link ${name}`);
      }
      if (!inside(linkTarget(directory, link))) throw new RuntimeError("runtime_archive_unsafe", `link ${name}`);
      fs.symlinkSync(link, target);
    } else {
      throw new RuntimeError("runtime_archive_unsafe", `entry type ${name}`);
    }
  }
  // Each link was checked when it was made, against the links unpacked before it. A later
  // entry can change where an earlier link leads (d/a -> b/c/../../.. is inside until d/b -> ..
  // appears), so the finished tree is checked once more: every link must lead inside.
  const verify = (directory) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const entryPath = path.join(directory, entry.name);
      if (entry.isSymbolicLink()) {
        if (!inside(linkTarget(directory, fs.readlinkSync(entryPath)))) {
          throw new RuntimeError("runtime_archive_unsafe", `link ${path.relative(realRoot, entryPath)}`);
        }
      } else if (entry.isDirectory()) {
        verify(entryPath);
      }
    }
  };
  verify(realRoot);
}

export const PROBE_SCRIPT = "import ssl, sqlite3, sys; "
  + "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey; "
  + "sys.exit(0 if sys.version_info >= (3, 11) else 1)";

/** The environment a runtime interpreter is started with: no PYTHON* variable reaches it. */
export function isolatedEnvironment(environment = process.env) {
  return Object.fromEntries(Object.entries(environment).filter(([key]) => !key.toUpperCase().startsWith("PYTHON")));
}

/**
 * Runs the unpacked interpreter the way the clients will: isolated from user site packages.
 * `spawn` is what starts a program on this computer (the tests model a Windows interpreter,
 * which no test can write as a file the way it writes a shell script).
 */
export function probe(root, manifest, { spawn = spawnSync } = {}) {
  const executable = path.join(root, ...normalComponents(manifest.executable));
  const installer = path.join(root, ...normalComponents(manifest.installer));
  if (!isFile(executable) || !isFile(installer)) throw new RuntimeError("runtime_probe_failed", "entry point missing");
  const result = spawn(executable, ["-E", "-s", "-c", PROBE_SCRIPT], {
    env: isolatedEnvironment(), encoding: "utf8", windowsHide: true, timeout: 120_000,
  });
  if (result.status !== 0) {
    const last = String(result.stderr || result.error?.code || "exit status").trim().split("\n").pop().slice(0, 300);
    throw new RuntimeError("runtime_probe_failed", last);
  }
}

function isFile(target) {
  try {
    return fs.statSync(target).isFile();
  } catch {
    return false;
  }
}

function writeJsonAtomically(target, value) {
  const temporary = target.replace(/\.json$/, ".json.partial");
  fs.writeFileSync(temporary, JSON.stringify(value, null, 2));
  fs.renameSync(temporary, target);
}

/** A pointer file as Desktop Companion writes it; null unless both entry points still exist. */
export function readPointer(base, name) {
  let pointer;
  try {
    pointer = JSON.parse(fs.readFileSync(path.join(base, name), "utf8"));
  } catch {
    return null;
  }
  if (pointer === null || typeof pointer !== "object"
      || !["runtimeId", "root", "executable", "installer"].every((field) => typeof pointer[field] === "string")
      || !Array.isArray(pointer.capabilities) || !pointer.capabilities.every((item) => typeof item === "string")) {
    return null;
  }
  return isFile(pointer.executable) && isFile(pointer.installer) ? pointer : null;
}

export function current(base) {
  return readPointer(base, "current.json");
}

/**
 * Installs a verified archive as the current runtime; the old current becomes previous and
 * older runtimes are removed. Nothing switches unless the new runtime passes its probe.
 */
export function installVerified(base, manifest, bytes, { spawn = spawnSync } = {}) {
  checkArchive(bytes, manifest);
  fs.mkdirSync(base, { recursive: true });
  const root = path.join(base, manifest.runtimeId);
  if (!isFile(path.join(root, ...normalComponents(manifest.executable)))) {
    // Desktop Companion may be installing the same runtime at this moment: each stages in its
    // own directory, and whoever renames second finds the finished runtime and keeps it.
    const staging = path.join(base, `${manifest.runtimeId}.partial-${process.pid}`);
    fs.rmSync(staging, { recursive: true, force: true });
    try {
      // The signed description says which computer the runtime is for, and so what it may hold.
      extractArchive(bytes, staging, { windows: manifest.platform.startsWith("windows-") });
      probe(staging, manifest, { spawn });
      fs.rmSync(root, { recursive: true, force: true });
      fs.renameSync(staging, root);
    } finally {
      fs.rmSync(staging, { recursive: true, force: true });
    }
  }
  probe(root, manifest, { spawn });
  const pointer = {
    runtimeId: manifest.runtimeId,
    root,
    executable: path.join(root, ...normalComponents(manifest.executable)),
    installer: path.join(root, ...normalComponents(manifest.installer)),
    capabilities: manifest.capabilities,
    // Not read by Desktop Companion up to 0.4.3, which also drops it when it rewrites the
    // pointer; `builtAt` then falls back to the day in the runtime id.
    createdAt: manifest.createdAt,
  };
  const previous = current(base);
  if (previous && previous.runtimeId !== pointer.runtimeId) writeJsonAtomically(path.join(base, "previous.json"), previous);
  writeJsonAtomically(path.join(base, "current.json"), pointer);
  const keep = [pointer.runtimeId, readPointer(base, "previous.json")?.runtimeId];
  for (const entry of fs.readdirSync(base, { withFileTypes: true })) {
    if (!entry.isDirectory() || keep.includes(entry.name)) continue;
    // Another process (a second terminal, Desktop Companion) may be unpacking into its own
    // staging directory right now; only one left behind long ago is removed.
    if (entry.name.includes(".partial")) {
      const age = Date.now() - (fs.statSync(path.join(base, entry.name), { throwIfNoEntry: false })?.mtimeMs ?? Date.now());
      if (age < STALE_STAGING_MS) continue;
    }
    try {
      fs.rmSync(path.join(base, entry.name), { recursive: true, force: true });
    } catch {
      // An older runtime still in use (Windows keeps a running interpreter's files): it is
      // removed by a later install, as Desktop Companion leaves it too.
    }
  }
  return pointer;
}

/** GET over HTTPS without redirects and within `limit` bytes. Any failure to get an answer is `offline`. */
export async function fetchLimited(url, limit, fetcher = globalThis.fetch) {
  if (new URL(url).protocol !== "https:") throw new RuntimeError("runtime_download_failed", "https is required");
  let response;
  try {
    response = await fetcher(url, { redirect: "error", signal: AbortSignal.timeout(600_000) });
  } catch {
    throw new RuntimeError("runtime_download_failed", "no answer", true);
  }
  if (!response.ok) throw new RuntimeError("runtime_download_failed", `HTTP ${response.status}`, response.status >= 500);
  if (Number(response.headers.get("content-length") ?? 0) > limit) {
    response.body?.cancel().catch(() => {});
    throw new RuntimeError("runtime_download_failed", "too large");
  }
  return readLimited(response.body, limit);
}

/**
 * The body of an answer, counted while it arrives. Without a Content-Length nothing else bounds
 * it: read whole before it is measured, a server could fill the memory of this computer. The
 * transfer is cancelled at the first byte over the limit.
 */
async function readLimited(body, limit) {
  if (!body) return Buffer.alloc(0);
  const reader = body.getReader();
  const chunks = [];
  let received = 0;
  for (;;) {
    let step;
    try {
      step = await reader.read();
    } catch {
      throw new RuntimeError("runtime_download_failed", "interrupted", true);
    }
    if (step.done) return Buffer.concat(chunks, received);
    received += step.value.byteLength;
    if (received > limit) {
      reader.cancel().catch(() => {});
      throw new RuntimeError("runtime_download_failed", "too large");
    }
    chunks.push(Buffer.from(step.value.buffer, step.value.byteOffset, step.value.byteLength));
  }
}

/**
 * Returns the current runtime, installing the published one first when it is newer or missing:
 * {pointer, offline, kept}. `kept` says why an installed runtime was used although another one
 * is published: "download" (its archive could not be fetched) or "older" (it was built before
 * the installed one).
 *
 * Without the network, or when the new archive cannot be fetched, the runtime already installed
 * is used: it passed the same checks when it was installed, and seeing the status, leaving a
 * connection and uninstalling have to work then. A validly signed older runtime never replaces
 * a newer one: a signature does not expire, so whoever serves the index could otherwise hand a
 * computer any runtime ever published. A published runtime that fails a check of its content
 * is an error whatever is installed.
 */
export async function ensureRuntime({ home, origin, keys, platform = platformKey(), fetcher, onDownload, spawn = spawnSync }) {
  if (!platform) throw new RuntimeError("runtime_unsupported_platform");
  const base = runtimeBase(home);
  let index;
  try {
    const raw = await fetchLimited(`${origin}${RUNTIME_INDEX_PATH}`, MAX_INDEX_BYTES, fetcher);
    try {
      index = JSON.parse(raw.toString("utf8"));
    } catch {
      throw new RuntimeError("runtime_index_invalid", "not JSON");
    }
  } catch (error) {
    const installed = error.offline ? current(base) : null;
    if (installed) return { pointer: installed, offline: true, kept: null };
    throw error;
  }
  const selected = selectFromIndex(index, keys, platform);
  const installed = current(base);
  if (installed?.runtimeId === selected.manifest.runtimeId) return { pointer: installed, offline: false, kept: null };
  const installedTime = installed ? builtAt(installed) : null;
  if (installedTime !== null && Date.parse(selected.manifest.createdAt) < installedTime) {
    return { pointer: installed, offline: false, kept: "older" };
  }
  onDownload?.(selected.manifest);
  let bytes;
  try {
    bytes = await fetchLimited(`${origin}${selected.path}`, selected.manifest.archiveSize, fetcher);
  } catch (error) {
    if (installed && error.code === "runtime_download_failed") return { pointer: installed, offline: false, kept: "download" };
    throw error;
  }
  return { pointer: installVerified(base, selected.manifest, bytes, { spawn }), offline: false, kept: null };
}
