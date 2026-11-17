"""Runs the host gh wrapper's main with test paths: drive.py SCRIPT STATE WRAPPER BROKER [ARG]...
HOST_GH_CHECK_EVERY and HOST_GH_REFRESH_AFTER, when set, shorten its intervals. DRIVE_OFF_MAIN_SIGNAL,
a signal number, is sent to a thread other than the main one once the wrapper handles it and gh
logged ready."""

import importlib.util
import json
import os
import signal
import sys
import threading
import time


def off_main(signum: int) -> None:
    while signal.getsignal(signum) is signal.SIG_DFL or not ready():
        time.sleep(0.01)
    signal.pthread_kill(threading.get_ident(), signum)


def ready() -> bool:
    try:
        with open(os.environ["FAKE_GH_LOG"], encoding="utf-8") as f:
            return any(json.loads(line).get("ready") for line in f)
    except FileNotFoundError:
        return False


def main():
    script, state, wrapper, broker, *argv = sys.argv[1:]
    spec = importlib.util.spec_from_file_location("host_gh", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if signum := os.environ.get("DRIVE_OFF_MAIN_SIGNAL"):
        threading.Thread(target=off_main, args=(int(signum),), daemon=True).start()
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
