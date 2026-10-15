"""A computer never installs a release the platform withdrew, even when the service offers it
with a valid signature."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "adapters/client/scripts"))
sys.path.insert(0, str(ROOT / "tools"))

import sync_client  # noqa: E402

OLD = "20260901-101010-aaaaaaaaaaaa-bbbbbbbbbbbb"
OLDER = "20260801-101010-cccccccccccc-bbbbbbbbbbbb"
FINE = "20260915-101010-dddddddddddd-bbbbbbbbbbbb"


class WithdrawnReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # What the client says is read below in English, whatever the language of this computer.
        self.enterContext(patch.dict(os.environ, {"ARTIFICIAL_MEMORY_LANG": "en"}))
        self.state = Path(temporary.name) / "client"
        self.release = Path(temporary.name) / "release"

    def publish(self, *withdrawn: str) -> None:
        path = self.release / sync_client.WITHDRAWN_POLICY
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "withdrawn": [
            {"releaseId": item, "reason": "a hole"} for item in withdrawn]}), encoding="utf-8")

    def test_what_a_release_lists_is_remembered_and_refused_from_then_on(self) -> None:
        self.assertEqual(sync_client.known_withdrawn_releases(self.state), set())
        self.publish(OLD)
        known = sync_client.learn_withdrawn_releases(self.release, self.state)
        self.assertEqual(known, {OLD})
        with self.assertRaisesRegex(RuntimeError, f"release {OLD} was withdrawn by the platform and is not installed"):
            sync_client.refuse_withdrawn_release(OLD, sync_client.known_withdrawn_releases(self.state))
        with patch.dict(os.environ, {"ARTIFICIAL_MEMORY_LANG": "ru"}), \
                self.assertRaisesRegex(RuntimeError, f"выпуск {OLD} отозван платформой и не устанавливается"):
            sync_client.refuse_withdrawn_release(OLD, sync_client.known_withdrawn_releases(self.state))
        # An older release that was not withdrawn can still be gone back to.
        sync_client.refuse_withdrawn_release(FINE, sync_client.known_withdrawn_releases(self.state))

    def test_the_memory_only_grows(self) -> None:
        self.publish(OLD, OLDER)
        sync_client.learn_withdrawn_releases(self.release, self.state)
        # A later (or forged older) release with a shorter list takes nothing back.
        self.publish()
        self.assertEqual(sync_client.learn_withdrawn_releases(self.release, self.state), {OLD, OLDER})
        (self.release / sync_client.WITHDRAWN_POLICY).unlink()
        self.assertEqual(sync_client.learn_withdrawn_releases(self.release, self.state), {OLD, OLDER})

    def test_a_malformed_list_stops_the_installation(self) -> None:
        path = self.release / sync_client.WITHDRAWN_POLICY
        path.parent.mkdir(parents=True)
        for bad in ('{"withdrawn": "all"}', '{"withdrawn": [{"releaseId": "../x"}]}', '{"withdrawn": ["' + OLD + '"]}'):
            path.write_text(bad, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "list of withdrawn releases is malformed"):
                sync_client.learn_withdrawn_releases(self.release, self.state)

    def test_the_sync_asks_the_list_twice(self) -> None:
        # That the list itself is in every release is checked where the list is kept, with the
        # rest of what a release delivers.
        orchestrator = (ROOT / "adapters/client/scripts/control_plane_sync.py").read_text(encoding="utf-8")
        before, after = orchestrator.index("refuse_withdrawn(desired"), orchestrator.index("learn_withdrawn(platform_root)")
        self.assertLess(before, orchestrator.index("stage_release("))
        self.assertLess(orchestrator.index("extract_verified(args"), after)
        self.assertLess(after, orchestrator.index('stage = "command_claim"'))


if __name__ == "__main__":
    unittest.main()
