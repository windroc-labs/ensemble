"""Runs the host gh wrapper's main with test paths: drive.py SCRIPT STATE WRAPPER BROKER [ARG]...
HOST_GH_CHECK_EVERY and HOST_GH_REFRESH_AFTER, when set, shorten its intervals."""

import importlib.util
import os
import sys


def main():
    script, state, wrapper, broker, *argv = sys.argv[1:]
    spec = importlib.util.spec_from_file_location("host_gh", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main(
        argv,
        state=state,
        wrapper=wrapper,
        broker=broker,
        every=float(os.environ.get("HOST_GH_CHECK_EVERY") or module.CHECK_EVERY),
        after=float(os.environ.get("HOST_GH_REFRESH_AFTER") or module.REFRESH_AFTER),
    )


if __name__ == "__main__":
    main()
