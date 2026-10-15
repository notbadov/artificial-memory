"""The steps of the verification page are run, not only written.

`docs/VERIFYING.md` tells a person how to check a downloaded runtime and a release by hand.
A page like that goes stale silently: the code changes what it signs and the page keeps its
old script. So every Python script on the page is taken from the page itself and run here
against an envelope and an archive made for the test, and the result is compared with what
the client's own code computes.

The page is at the top of the repository it is published in; a tree without it has nothing to
check here, and the test says so instead of passing.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from test_isolation import sandboxed_environment  # noqa: E402

PAGE = ROOT / "docs/VERIFYING.md"
SPKI_PREFIX = "MCowBQYDK2VwAyEA"  # base64 of 30 2a 30 05 06 03 2b 65 70 03 21 00


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TRANSPORT = load("release_transport_for_the_page", "adapters/client/scripts/release_transport.py")


def scripts(page: str) -> dict[str, str]:
    """File name -> source, for every script the page asks the reader to save."""
    found = {}
    for match in re.finditer(r"`(\w+\.py)`[^`]*?:\n\n( *)```python\n(.*?)\n\2```", page, re.S):
        indent = match.group(2)
        found[match.group(1)] = "\n".join(line[len(indent):] for line in match.group(3).split("\n")) + "\n"
    return found


@unittest.skipUnless(PAGE.is_file(), "docs/VERIFYING.md is not in this tree")
class VerifyingPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page = PAGE.read_text(encoding="utf-8")
        self.scripts = scripts(self.page)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        for name, source in self.scripts.items():
            (self.work / name).write_text(source, encoding="utf-8")

    def run_script(self, name: str, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, name, *arguments], cwd=self.work, capture_output=True, text=True,
                              encoding="utf-8", check=False, timeout=120, env=sandboxed_environment(self.work / "home"), errors="replace")

    def test_the_page_has_the_scripts_it_names(self) -> None:
        self.assertEqual(sorted(self.scripts),
                         ["check_release.py", "release_message.py", "runtime_message.py", "verify_signature.py"])

    def test_the_key_on_the_page_is_the_key_the_code_trusts(self) -> None:
        launcher = (ROOT / "packages/command-line/src/trust.mjs").read_text(encoding="utf-8")
        client = (ROOT / "adapters/client/scripts/command_line.py").read_text(encoding="utf-8")
        (key_id, key), = re.findall(r'"([a-z0-9-]+)": "([A-Za-z0-9+/]{43}=)"', launcher)
        self.assertIn(f'TRUSTED_KEYS = {{"{key_id}": "{key}"}}', client)
        self.assertIn(f"| Key id | `{key_id}` |", self.page)
        self.assertIn(f"`{key}`", self.page)
        # The PEM form on the page is the same 32 bytes behind the fixed Ed25519 prefix.
        self.assertIn(f"'{SPKI_PREFIX}{key}'", self.page)
        self.assertEqual(base64.b64decode(SPKI_PREFIX).hex(), "302a300506032b6570032100")

    def test_the_runtime_steps_verify_a_signed_runtime_and_refuse_a_changed_one(self) -> None:
        # The fixture carries the bytes the build tool signed, so the page's script is held to
        # the same bytes as the launcher and the desktop application.
        fixture = json.loads((ROOT / "packages/command-line/fixtures/runtime-envelope.json").read_text(encoding="utf-8"))
        platform = fixture["envelope"]["manifest"]["platform"]
        (self.work / "latest.json").write_text(json.dumps({"platforms": {platform: {"envelope": fixture["envelope"]}}}),
                                               encoding="utf-8")
        done = self.run_script("runtime_message.py", "latest.json", platform)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((self.work / "message.bin").read_bytes(), base64.b64decode(fixture["signingBytes"]))
        manifest = fixture["envelope"]["manifest"]
        self.assertIn(f"/downloads/client-runtime/{manifest['runtimeId']}/{manifest['archive']}", done.stdout)
        self.assertIn(manifest["archiveSha256"], done.stdout)
        self.assertIn(str(manifest["archiveSize"]), done.stdout)

        valid = self.run_script("verify_signature.py", fixture["publicKey"], "message.bin", "signature.bin")
        self.assertEqual((valid.returncode, valid.stdout.strip()), (0, "signature is valid"), valid.stderr)
        self.openssl_agrees(fixture["publicKey"], expected=True)

        (self.work / "message.bin").write_bytes((self.work / "message.bin").read_bytes().replace(b"aarch64", b"x86_64_"))
        changed = self.run_script("verify_signature.py", fixture["publicKey"], "message.bin", "signature.bin")
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("InvalidSignature", changed.stderr)
        self.openssl_agrees(fixture["publicKey"], expected=False)

    def openssl_agrees(self, public_key: str, expected: bool) -> None:
        """The page's `openssl` command, where an OpenSSL 3 is installed; the Python check above
        is the one that always runs."""
        openssl = shutil.which("openssl")
        version = subprocess.run([openssl, "version"], capture_output=True, text=True, check=False, encoding="utf-8", errors="replace").stdout if openssl else ""
        if not version.startswith("OpenSSL 3"):
            return
        command = re.search(r"openssl pkeyutl -verify [^\n]+", self.page).group().split()
        (self.work / "release-key.pem").write_text(
            f"-----BEGIN PUBLIC KEY-----\n{SPKI_PREFIX}{public_key}\n-----END PUBLIC KEY-----\n", encoding="ascii")
        done = subprocess.run([openssl, *command[1:]], cwd=self.work, capture_output=True, text=True, check=False,
                              env=sandboxed_environment(self.work / "home"), encoding="utf-8", errors="replace")
        self.assertEqual(done.returncode == 0, expected, done.stdout + done.stderr)
        if expected:
            self.assertIn("Signature Verified Successfully", done.stdout)

    def release(self) -> tuple[dict, Path]:
        key = Ed25519PrivateKey.generate()
        inner = {"release_id": "release-1", "layout": {"agent-platform": "agent-platform"},
                 "sources": {"agent-platform": {"sha": "a" * 40}, "home-server-monorepo": {"sha": "b" * 40}}}
        files = {"manifest.json": json.dumps(inner).encode(), "agent-platform/rule.md": "Правило\n".encode("utf-8")}
        files["checksums.sha256"] = "".join(
            f"{hashlib.sha256(body).hexdigest()}  {path}\n" for path, body in files.items()).encode()
        archive = self.work / "release.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for path, body in files.items():
                info = tarfile.TarInfo("release-1/" + path)
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
        manifest = {"releaseId": "release-1", "agentPlatformSha": "a" * 40, "homeServerSha": "b" * 40,
                    "bundleSha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "bundleSize": archive.stat().st_size,
                    "createdAt": "2026-10-13T09:00:00Z", "minClientProtocol": 1}
        envelope = {"schemaVersion": 1, "keyId": "test", "manifest": manifest}
        envelope["signature"] = base64.b64encode(key.sign(TRANSPORT.signing_bytes(envelope))).decode()
        (self.work / "trust.json").write_text(json.dumps({"version": 1, "keys": {"test": {
            "status": "active", "publicKey": base64.b64encode(key.public_key().public_bytes_raw()).decode()}}}), encoding="utf-8")
        return envelope, archive

    def test_the_release_steps_agree_with_the_client(self) -> None:
        envelope, archive = self.release()
        # What the client accepts ...
        manifest = TRANSPORT.verify_release(envelope, self.work / "trust.json")
        TRANSPORT.inspect_bundle(archive, manifest)
        # ... the page's scripts accept, from the same bytes.
        (self.work / "envelope.json").write_text(json.dumps(envelope), encoding="utf-8")
        done = self.run_script("release_message.py", "envelope.json")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((self.work / "message.bin").read_bytes(), TRANSPORT.signing_bytes(envelope))
        self.assertIn(manifest["bundleSha256"], done.stdout)
        public_key = json.loads((self.work / "trust.json").read_text(encoding="utf-8"))["keys"]["test"]["publicKey"]
        valid = self.run_script("verify_signature.py", public_key, "message.bin", "signature.bin")
        self.assertEqual((valid.returncode, valid.stdout.strip()), (0, "signature is valid"), valid.stderr)
        checked = self.run_script("check_release.py", str(archive))
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn("release release-1 - 2 files match their checksums", checked.stdout)

    def test_the_release_check_refuses_a_changed_or_an_unlisted_file(self) -> None:
        _, archive = self.release()
        for name, body, expected in (("release-1/agent-platform/rule.md", b"changed\n", "checksum does not match"),
                                     ("release-1/agent-platform/added.py", b"print()\n", "files without a checksum")):
            changed = self.work / "changed.tar.gz"
            with tarfile.open(archive, "r:gz") as source, tarfile.open(changed, "w:gz") as target:
                for member in source:
                    if member.name != name:
                        target.addfile(member, source.extractfile(member))
                info = tarfile.TarInfo(name)
                info.size = len(body)
                target.addfile(info, io.BytesIO(body))
            done = self.run_script("check_release.py", str(changed))
            self.assertNotEqual(done.returncode, 0, name)
            self.assertIn(expected, done.stderr)
            with self.assertRaises(ValueError):
                TRANSPORT.inspect_bundle(changed, {"releaseId": "release-1", "agentPlatformSha": "a" * 40,
                                                   "homeServerSha": "b" * 40})

    def test_what_the_page_says_waits_for_approval_is_what_the_client_holds_back(self) -> None:
        # A skill of markdown alone can grant itself tools, declare hooks or run a command each
        # time it is invoked. The page names each of them, and each is held back by the client's
        # own check.
        section = self.page.split("## What is not signed with the release key", 1)[1]
        for named in ("`allowed-tools`", "`hooks`", "!`"):
            self.assertIn(named, section)
        sys.path.insert(0, str(ROOT / "adapters/client/scripts"))
        consents = load("sync_consents_for_the_page", "adapters/client/scripts/sync_consents.py")
        for name, body in (("tools", "---\nallowed-tools: Bash\n---\n"), ("hooks", "---\nhooks:\n  Stop: []\n---\n"),
                           ("command", "Now: !`date`\n"), ("text", "---\nname: text\n---\n# Only words\n")):
            skill = self.work / "skills" / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(body, encoding="utf-8")
            with self.subTest(name):
                key = consents.skill_consent_key(name, skill)
                self.assertEqual(key is None, name == "text", key)


if __name__ == "__main__":
    unittest.main()
