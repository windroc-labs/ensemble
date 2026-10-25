"""A `python3` for bin/ensemble-orchestrator in tests: runs `-P -m MODULE ARGS` as python3 would, under
the fake Linear transport ($FAKE_LINEAR_TRANSPORT), so the router and its write-back callback reach
only the fake service. Its shim adds the test roots to PYTHONPATH.
"""

import os
import runpy
import shlex
import sys

from tests.orchestrator.integration.fake_transport import install, refuse_keychain

FAKE_PYTHON = os.path.abspath(__file__)
TESTS = os.path.dirname(os.path.dirname(os.path.dirname(FAKE_PYTHON)))
REPO = os.path.dirname(TESTS)


def write_shim(directory):
    """`<directory>/python3`: this interpreter on this file with the test roots appended to
    PYTHONPATH; returns directory, for PATH.
    """
    os.makedirs(directory, exist_ok=True)
    with open(shim := os.path.join(directory, "python3"), "w", encoding="utf-8") as f:
        f.write(
            f'#!/bin/sh\nPYTHONPATH="$PYTHONPATH:{TESTS}:{REPO}" '
            f'exec {shlex.quote(sys.executable)} -P -s {shlex.quote(FAKE_PYTHON)} "$@"\n'
        )
    os.chmod(shim, 0o755)
    return directory


def main(argv):
    if argv[:2] != ["-P", "-m"] or len(argv) < 3:
        sys.exit(f"fake_python.py: want -P -m MODULE [ARG ...], got {argv}")
    sys.argv = [argv[2], *argv[3:]]
    sys.addaudithook(refuse_keychain)
    with install(os.environ["FAKE_LINEAR_TRANSPORT"]):
        runpy.run_module(argv[2], run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main(sys.argv[1:])
