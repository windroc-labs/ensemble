#!/usr/bin/env python3
"""A gh for the host gh wrapper's tests: logs each run to $FAKE_GH_LOG, then acts as $FAKE_GH_MODE says:
exit:N, echo (stdin to stdout), die:SIGNUM, or wait:SECONDS (recording the signals it gets)."""

import json
import os
import signal
import sys
import time

argv = sys.argv[1:]
if argv == ["--version"]:
    print("gh version 2.102.0 (2026-09-30)")
    sys.exit(0)


def note(**fields):
    with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps(fields) + "\n")


note(
    argv=argv,
    int_ignored=signal.getsignal(signal.SIGINT) is signal.SIG_IGN,
    quit_ignored=signal.getsignal(signal.SIGQUIT) is signal.SIG_IGN,
)
mode, _, arg = os.environ.get("FAKE_GH_MODE", "exit:0").partition(":")
if mode == "exit":
    sys.exit(int(arg))
if mode == "echo":
    sys.stdout.write(sys.stdin.read())
    sys.exit(0)
if mode == "die":
    os.kill(os.getpid(), int(arg))
got = []
for signum in (signal.SIGTERM, signal.SIGINT):
    signal.signal(signum, lambda n, _: got.append(n))
note(ready=True)
deadline = time.monotonic() + float(arg)
while time.monotonic() < deadline:
    time.sleep(0.05)
note(signals=got)
sys.exit(5)
