from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import tarfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from test_isolation import sandboxed_environment  # noqa: E402

SPEC = importlib.util.spec_from_file_location("release_transport_test", ROOT / "adapters/client/scripts/release_transport.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
SYNC_SPEC = importlib.util.spec_from_file_location("sync_release_test", ROOT / "adapters/client/scripts/sync_client.py")
SYNC = importlib.util.module_from_spec(SYNC_SPEC)
SYNC_SPEC.loader.exec_module(SYNC)


class Response(io.BytesIO):
    def __init__(self, data, size):
        super().__init__(data)
        self.headers = {"Content-Length": str(size)}


class ReleaseTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.key = Ed25519PrivateKey.generate()
        self.trust = self.root / "trust.json"
        self.trust.write_text(json.dumps({"version": 1, "keys": {"test": {
            "status": "active", "publicKey": base64.b64encode(self.key.public_key().public_bytes_raw()).decode(),
        }}}), encoding="utf-8")
        self.manifest = {"releaseId": "release-1", "agentPlatformSha": "a"*40, "homeServerSha": "b"*40,
                         "bundleSha256": "c"*64, "bundleSize": 100, "createdAt": "2026-08-31T13:00:00Z", "minClientProtocol": 1}

    def signed(self, manifest=None):
        envelope = {"schemaVersion": 1, "keyId": "test", "manifest": manifest or self.manifest}
        envelope["signature"] = base64.b64encode(self.key.sign(MODULE.signing_bytes(envelope))).decode()
        return envelope

    def bundle(self, extra=None, omitted_checksum=False, wrong_identity=False):
        inner = {"release_id": "wrong" if wrong_identity else "release-1", "layout": {"agent-platform": "agent-platform"},
                 "sources": {"agent-platform": {"sha": "a"*40}, "home-server-monorepo": {"sha": "b"*40}}}
        files = {"manifest.json": json.dumps(inner).encode(), "agent-platform/test.py": b"print('must not execute')"}
        files["checksums.sha256"] = "".join(hashlib.sha256(body).hexdigest() + "  " + path + "\n"
                                           for path, body in files.items() if not (omitted_checksum and path.endswith("test.py"))).encode()
        archive_path = self.root / "fixture.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            for path, data in files.items():
                info = tarfile.TarInfo("release-1/" + path)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            if extra:
                archive.addfile(extra)
        return archive_path

    def desired(self, archive):
        manifest = self.manifest | {"bundleSize": archive.stat().st_size, "bundleSha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
        return {"release": manifest | {"envelope": self.signed(manifest)}}

    def test_signature_rejects_tampering_missing_revoked_or_replaced_key(self):
        envelope = self.signed()
        self.assertEqual(MODULE.verify_release(envelope, self.trust), self.manifest)
        changed = copy.deepcopy(envelope)
        changed["manifest"]["agentPlatformSha"] = "e"*40
        with self.assertRaises(ValueError): MODULE.verify_release(changed, self.trust)
        with self.assertRaises(ValueError): MODULE.verify_release(envelope, self.root / "missing.json")
        trust = json.loads(self.trust.read_text(encoding="utf-8"))
        trust["keys"]["test"]["status"] = "revoked"
        self.trust.write_text(json.dumps(trust), encoding="utf-8")
        with self.assertRaises(ValueError): MODULE.verify_release(envelope, self.trust)

    def test_unsupported_protocol_and_malformed_contract_fail_closed(self):
        for manifest in (self.manifest | {"minClientProtocol": 2}, self.manifest | {"bundleSize": True}, self.manifest | {"releaseId": "../bad"}):
            with self.assertRaises(ValueError): MODULE.verify_release(self.signed(manifest), self.trust)
        for envelope in (None, [], {}, self.signed() | {"schemaVersion": True}):
            with self.assertRaises(ValueError): MODULE.verify_release(envelope, self.trust)

    def test_stage_downloads_verifies_and_never_extracts_or_executes(self):
        archive = self.bundle()
        desired = self.desired(archive)
        cache = self.root / "cache"
        with mock.patch.object(MODULE, "open_request", return_value=Response(archive.read_bytes(), archive.stat().st_size)):
            result = MODULE.stage_release("https://control.test", "test", "secret", desired, self.trust, cache)
        self.assertEqual(result["status"], "staged")
        self.assertFalse(result["installed"])
        self.assertEqual(Path(result["bundlePath"]).read_bytes(), archive.read_bytes())
        self.assertEqual(len(list(cache.iterdir())), 1)

    def test_interruption_or_corruption_preserves_previous_cache_and_cleans_partial(self):
        archive = self.bundle()
        desired = self.desired(archive)
        cache = self.root / "cache"
        cache.mkdir()
        previous = cache / "last-known-good.tar.gz"
        previous.write_bytes(b"previous")
        for data, size in ((b"short", archive.stat().st_size), (bytes(archive.stat().st_size), archive.stat().st_size), (b"bad", 3)):
            with mock.patch.object(MODULE, "open_request", return_value=Response(data, size)):
                with self.assertRaises(ValueError): MODULE.stage_release("https://control.test", "test", "secret", desired, self.trust, cache)
            self.assertEqual(list(cache.iterdir()), [previous])
            self.assertEqual(previous.read_bytes(), b"previous")

    def test_invalid_signature_prevents_any_download(self):
        archive = self.bundle()
        desired = self.desired(archive)
        desired["release"]["envelope"]["signature"] = base64.b64encode(bytes(64)).decode()
        with mock.patch.object(MODULE, "open_request") as request:
            with self.assertRaises(ValueError): MODULE.stage_release("https://control.test", "test", "secret", desired, self.trust, self.root / "cache")
            request.assert_not_called()

    def test_signed_inner_identity_and_complete_checksum_coverage_required(self):
        for options in ({"wrong_identity": True}, {"omitted_checksum": True}):
            with self.assertRaises(ValueError): MODULE.inspect_bundle(self.bundle(**options), self.manifest)

    def test_archive_rejects_links_traversal_duplicate_paths_and_windows_paths(self):
        for name, kind in (("release-1/../../escape", tarfile.REGTYPE), ("release-1/link", tarfile.SYMTYPE),
                           ("release-1/hard", tarfile.LNKTYPE), ("C:\\escape", tarfile.REGTYPE),
                           ("release-1/manifest.json", tarfile.REGTYPE), ("release-1/CON", tarfile.REGTYPE)):
            extra = tarfile.TarInfo(name)
            extra.type = kind
            extra.linkname = "../../escape" if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ""
            with self.assertRaises(ValueError): MODULE.inspect_bundle(self.bundle(extra=extra), self.manifest)

    def test_https_and_no_redirect_policy(self):
        for url in ("http://control.test", "https://user:pass@control.test", "https://control.test?token=x"):
            with self.assertRaises(ValueError): MODULE.open_request(url, "/v1/client-session/desired", {})
        with self.assertRaises(ValueError): MODULE.NoRedirect().redirect_request(None, None, 307, "", {}, "https://other.test")

    @unittest.skipUnless(sys.platform == "darwin" or os.name == "nt", "the installer of a runtime refuses any other system first")
    def test_a_refused_address_reaches_a_person_in_the_language_of_the_computer(self):
        # The installer of a runtime, as the terminal client and Desktop Companion start it,
        # names the refusal of the transport in its answer; nothing chooses the language but
        # the computer's own.
        cleared = ("ARTIFICIAL_MEMORY_LANG", "LC_ALL", "LC_MESSAGES", "LANG")
        for language, expected in (("en_US.UTF-8", "Control Plane needs an HTTPS address without credentials or parameters."),
                                   ("ru_RU.UTF-8", "Control Plane требует HTTPS-адрес без учётных данных и параметров.")):
            environment = {key: value for key, value in sandboxed_environment(self.root).items() if key not in cleared}
            completed = subprocess.run(
                [sys.executable, "-E", "-s", str(ROOT / "adapters/client/scripts/companion_install.py"),
                 "--control-plane-url", "http://service.test/agent-api", "--client-id", "test-computer",
                 "--display-name", "Computer", "--provider", "codex", "--release-trust-file", str(self.trust)],
                env={**environment, "LANG": language, "AGENT_PLATFORM_CLIENT_TOKEN": "private-test-token"},
                cwd=self.root, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=120)
            with self.subTest(language=language):
                self.assertEqual(completed.returncode, 1, completed.stderr)
                self.assertEqual(json.loads(completed.stdout), {"status": "failed", "detail": expected})

    def test_default_client_targets_the_production_control_plane_without_legacy_transport(self):
        with mock.patch("sys.argv", ["sync_client.py"]):
            args = SYNC.parse_args()
        self.assertEqual(args.control_plane_url, "https://app.artificial-memory.com/agent-api")
        self.assertFalse(hasattr(args, "transport"))
        self.assertFalse(hasattr(args, "remote_host"))

    def test_v1_stage_does_not_touch_scheduled_tasks_credentials_or_providers(self):
        argv = ["sync_client.py", "--stage-only", "--client-id", "test",
                "--control-plane-url", "https://control.test", "--release-trust-file", str(self.trust), "--client-token", "fixture"]
        with mock.patch("sys.argv", argv), mock.patch.object(SYNC, "desired_state", return_value={}), mock.patch.object(SYNC, "release_transport") as transport, \
                mock.patch.object(SYNC, "run_installers") as installers, mock.patch.object(SYNC, "ensure_scheduled_task") as task, \
                mock.patch.object(SYNC, "configure_agent_memory") as memory, mock.patch.object(SYNC, "report_state") as report:
            transport.return_value.stage_release.return_value = {"status": "staged", "installed": False}
            self.assertEqual(SYNC.main(), 0)
            for untouched in (installers, task, memory, report): untouched.assert_not_called()
