"""Run the requested real test runner with isolated computer-client state."""

import argparse
import contextlib
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.test_isolation import isolated_client_state, sandboxed_home  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sandbox-home", action="store_true",
        help="run the whole runner with a disposable home (HOME, USERPROFILE, APPDATA, LOCALAPPDATA). "
             "Off by default: the suites that need it sandbox themselves, and others were never run this way.")
    parser.add_argument("runner", choices=("unittest", "pytest"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    with contextlib.ExitStack() as stack:
        if arguments.sandbox_home:
            stack.enter_context(sandboxed_home())
        stack.enter_context(isolated_client_state())
        return subprocess.call([sys.executable, "-m", arguments.runner, *arguments.arguments], cwd=ROOT,
                               env=dict(os.environ))


if __name__ == "__main__":
    raise SystemExit(main())
