# Verifying what you download

Two things reach a computer and run there: the **client runtime** (a standalone Python with
the terminal client) and the **release** (the synchronization client, the adapters, and the
skills and rules for the AI apps). Both are signed with the same release key, and the code in
this repository refuses either when the signature or a checksum does not match. This page
shows how to make the same checks by hand.

The commands below were run on macOS with Python 3.12 and OpenSSL 3. They need Python 3.9 or
newer and nothing installed into it. The `openssl` that macOS itself ships is LibreSSL, which
cannot verify a raw Ed25519 message this way; use OpenSSL 3 (for example from Homebrew) or the
Python alternative given below. The steps were not run on Windows.

## The release key

| | |
|---|---|
| Algorithm | Ed25519 |
| Key id | `home-server-2026-09` |
| Public key, base64 of the 32 raw bytes | `0j9MtkDqgSW5Z+tjC6aSv/5X2qGDZA9nYoJyqJbhZP8=` |

The key is compiled into everything that verifies with it, and nothing received from the
network can add or replace a key:

- the npm launcher: `packages/command-line/src/trust.mjs`;
- the terminal client: `TRUSTED_KEYS` in `adapters/client/scripts/command_line.py`;
- the desktop application carries the same key.

A connected computer also keeps the key in `~/.agent-platform/client/release-trust.json`;
the synchronization client reads it from there.

For `openssl`, the same key as a PEM file (the fixed 12-byte Ed25519 prefix
`MCowBQYDK2VwAyEA`, then the key):

```sh
printf '%s\n' '-----BEGIN PUBLIC KEY-----' \
  'MCowBQYDK2VwAyEA0j9MtkDqgSW5Z+tjC6aSv/5X2qGDZA9nYoJyqJbhZP8=' \
  '-----END PUBLIC KEY-----' > release-key.pem
```

## The client runtime

### What is signed

The service publishes an index at
`https://app.artificial-memory.com/downloads/client-runtime/latest.json`. For each platform
(`darwin-aarch64`, `darwin-x86_64`, `windows-x86_64`) it holds an envelope:

```json
{"schemaVersion": 1, "keyId": "…", "manifest": {…}, "signature": "…"}
```

The signature covers these bytes: the text `agent-platform-client-runtime-v1`, one zero byte,
and then the JSON of `{"keyId", "manifest", "schemaVersion"}` with keys sorted and no spaces.
The manifest names the archive, its size and its SHA-256, the Python it contains and the
commit it was built from. So the signature fixes the exact bytes of the archive; the archive
itself carries no signature. The address of the archive is built from the signed manifest, not
taken from the index.

The code: `verifyEnvelope` and `checkArchive` in `packages/command-line/src/runtime.mjs`.

### By hand

1. Get the index.

   ```sh
   curl -fsS -o latest.json https://app.artificial-memory.com/downloads/client-runtime/latest.json
   ```

2. Write out the signed bytes and the signature, and print what the manifest promises. Save
   this as `runtime_message.py`:

   ```python
   import base64
   import json
   import sys

   index, platform = sys.argv[1], sys.argv[2]
   with open(index, encoding="utf-8") as stream:
       envelope = json.load(stream)["platforms"][platform]["envelope"]
   manifest = envelope["manifest"]
   signed = {name: envelope[name] for name in ("schemaVersion", "keyId", "manifest")}
   message = b"agent-platform-client-runtime-v1\x00" + json.dumps(
       signed, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("ascii")
   with open("message.bin", "wb") as stream:
       stream.write(message)
   with open("signature.bin", "wb") as stream:
       stream.write(base64.b64decode(envelope["signature"], validate=True))
   print("key id: ", envelope["keyId"])
   print("archive:", f"/downloads/client-runtime/{manifest['runtimeId']}/{manifest['archive']}")
   print("sha256: ", manifest["archiveSha256"])
   print("size:   ", manifest["archiveSize"])
   ```

   ```sh
   python3 runtime_message.py latest.json darwin-aarch64
   ```

   The key id must be the one in the table above.

3. Verify the signature with the key file made earlier.

   ```sh
   openssl pkeyutl -verify -pubin -inkey release-key.pem -rawin -in message.bin -sigfile signature.bin
   ```

   It prints `Signature Verified Successfully`. Anything else means the index was not signed
   with the release key.

   Without OpenSSL 3, the same check is made in Python with the `cryptography` package
   (`python3 -m pip install cryptography`). It prints `signature is valid` or raises
   `InvalidSignature`. Save this as `verify_signature.py`:

   ```python
   import base64
   import sys

   from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

   key, message, signature = sys.argv[1], sys.argv[2], sys.argv[3]
   with open(message, "rb") as stream:
       signed = stream.read()
   with open(signature, "rb") as stream:
       Ed25519PublicKey.from_public_bytes(base64.b64decode(key, validate=True)).verify(stream.read(), signed)
   print("signature is valid")
   ```

   ```sh
   python3 verify_signature.py 0j9MtkDqgSW5Z+tjC6aSv/5X2qGDZA9nYoJyqJbhZP8= message.bin signature.bin
   ```

4. Download the archive from `https://app.artificial-memory.com` plus the path printed in
   step 2, and compare its size and SHA-256 with the two printed values.

   ```sh
   wc -c < client-runtime-….tar.gz
   shasum -a 256 client-runtime-….tar.gz
   ```

A runtime that is already installed is in `~/.agent-platform/runtime/`. What the launcher does
besides these checks (it refuses links that lead out of that directory, and a runtime built
before the installed one) is in `packages/command-line/README.md`.

## A release

### What is signed

A connected computer asks the service which release it should have. The answer holds an
envelope of the same shape, signed with the same key over the text
`agent-platform-release-v1`, one zero byte, and the JSON of
`{"keyId", "manifest", "schemaVersion"}` with keys sorted, no spaces and non-ASCII characters
escaped. The manifest holds exactly these fields: `releaseId`, `agentPlatformSha`,
`homeServerSha`, `bundleSha256`, `bundleSize`, `createdAt`, `minClientProtocol`.

The client then downloads the archive and, before unpacking anything, checks that:

- its size and SHA-256 are the signed `bundleSize` and `bundleSha256`;
- it holds only regular files and directories under one top directory named `releaseId`, with
  no path that leaves it;
- `manifest.json` inside names the same release and the same two source commits as the signed
  manifest;
- `checksums.sha256` inside lists every other file, and every listed SHA-256 matches.

The code: `verify_release` and `inspect_bundle` in
`adapters/client/scripts/release_transport.py`.

### By hand

The envelope is given only to a connected computer, together with what that computer should
install, and the client does not keep it. The archive it does keep: a verified release is
stored as `~/.agent-platform/releases/<bundleSha256>.tar.gz`. So without the envelope you can
check that the archive is the one the signed manifest named, and that it is whole inside.

1. The name of the file is the SHA-256 the signature covered. Compare:

   ```sh
   ls ~/.agent-platform/releases/
   shasum -a 256 ~/.agent-platform/releases/*.tar.gz
   ```

2. Check every file inside against the list the archive carries. Save this as
   `check_release.py`:

   ```python
   import hashlib
   import sys
   import tarfile

   with tarfile.open(sys.argv[1], "r:gz") as archive:
       files = {member.name: member for member in archive if member.isfile()}
       roots = {name.split("/", 1)[0] for name in files}
       if len(roots) != 1:
           raise SystemExit("more than one top directory")
       root = roots.pop() + "/"
       listed = set()
       for line in archive.extractfile(files[root + "checksums.sha256"]).read().decode("utf-8").splitlines():
           digest, _, relative = line.partition("  ")
           actual = hashlib.sha256(archive.extractfile(files[root + relative]).read()).hexdigest()
           if actual != digest:
               raise SystemExit(f"checksum does not match: {relative}")
           listed.add(root + relative)
       unlisted = set(files) - listed - {root + "checksums.sha256"}
       if unlisted:
           raise SystemExit(f"files without a checksum: {sorted(unlisted)[:5]}")
   print("release", root.rstrip("/"), "-", len(listed), "files match their checksums")
   ```

   ```sh
   python3 check_release.py ~/.agent-platform/releases/<bundleSha256>.tar.gz
   ```

3. If you have an envelope (it is the `release.envelope` object of the answer to
   `/v1/client-session/desired`), its signature is checked like the runtime's. Save this
   as `release_message.py`, run it, then repeat step 3 of the runtime section:

   ```python
   import base64
   import json
   import sys

   with open(sys.argv[1], encoding="utf-8") as stream:
       envelope = json.load(stream)
   signed = {name: envelope[name] for name in ("schemaVersion", "keyId", "manifest")}
   message = b"agent-platform-release-v1\x00" + json.dumps(
       signed, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
   with open("message.bin", "wb") as stream:
       stream.write(message)
   with open("signature.bin", "wb") as stream:
       stream.write(base64.b64decode(envelope["signature"], validate=True))
   print("key id:", envelope["keyId"])
   print("sha256:", envelope["manifest"]["bundleSha256"])
   print("size:  ", envelope["manifest"]["bundleSize"])
   ```

## What is not signed with the release key

The skills and rules of your own space, and your personal ones, are written in the service by
you and the people you work with. They are not signed with the release key. When such a skill
contains anything but text and pictures, or text that makes an AI app run something, it waits
for your approval on the computer (`npx artificial-memory consents`); a background run approves
nothing. Text runs something when a markdown file's frontmatter has the key `allowed-tools`
(the skill grants itself tools) or `hooks` (commands run on the app's events), or when the text
runs a shell command as the skill is invoked: `!` right before a command in backticks
(`` !`git status` ``) or a fenced block opened with ```` ```! ````. Approval is given once for
the exact content; a changed skill waits again. The code:
`adapters/client/scripts/sync_consents.py`.

In Codex the client lets the tools that only read the space's memory run without asking
(`approval_mode = "approve"` for each of them in `~/.codex/config.toml`), so that a Codex started
without a person, such as `codex exec`, can use memory. Proposing a change to memory and the steps
of reviewing proposals still ask. The list is `READ_TOOL_NAMES` in
`tools/install_agent_memory_mcp.py`; a test holds it to the tools the memory service serves to a
read-only connection.

The task channel works the same way: the task tools that only read run without asking, and every
other task tool asks (`default_tools_approval_mode = "prompt"`). The list is
`COLLABORATION_READ_TOOL_NAMES` in `tools/install_app_agent_channels.py`, the tools the task
server marks `readOnlyHint`. These are values that Codex has accepted since before 0.144: every
Codex on the computer reads the same `~/.codex/config.toml`, and one that does not know a value
refuses the whole file. A server of your space that asks only before writes (`writes`, known to
Codex since 0.144.0) is written that way only when every Codex the client finds on the computer
is 0.144.0 or later; otherwise all its tools ask, and `artificial-memory status` names the Codex
to update. The code: `adapters/client/scripts/sync_codex_versions.py`.
