import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sync_guard", ROOT / "adapters/client/scripts/sync_guard.py"
)
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)


class SyncGuardTests(unittest.TestCase):
    def test_nonblocking_lock_allows_one_managed_client_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with GUARD.sync_guard(root):
                with self.assertRaises(GUARD.ClientBusy):
                    with GUARD.sync_guard(root):
                        pass
            with GUARD.sync_guard(root):
                pass

    def test_guard_has_no_migration_pause_protocol(self):
        source = (ROOT / "adapters/client/scripts/sync_guard.py").read_text(encoding="utf-8")
        self.assertNotIn("migration", source.lower())
        self.assertNotIn("pause", source.lower())


if __name__ == "__main__":
    unittest.main()
