"""A callback command for tests: `python3 -P -S callback_recorder.py LOG [--exit KIND:CODE]...
[--block KIND[:ATTEMPT]]`. Reads its event line from stdin, prints noise on stdout, appends one
JSON line to LOG: the stdin text, its cwd, pid, parent, process group and blocked signals, and
whether the callback lock in its cwd is held and what it says. --exit: exit CODE on an event of
KIND.
--block: on an event of KIND (of that ATTEMPT only, when given), start a grandchild in its
group, log its pid too, then sleep.
"""

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time

NOISE = "callback stdout noise"
LOCK = "delegate.callback.lock"
BLOCK = 600  # seconds: longer than any test waits
SLEEP = "import sys, time; time.sleep(int(sys.argv[1]))"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("log")
    parser.add_argument("--exit", action="append", default=[])
    parser.add_argument("--block")
    a = parser.parse_args(argv)
    line = sys.stdin.read()
    event = json.loads(line)
    kind = event["kind"]
    block, _, attempt = (a.block or "").partition(":")
    blocking = kind == block and attempt in ("", str(event["attempt"]))
    print(NOISE, flush=True)
    entry = {
        "line": line,
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "pgid": os.getpgid(0),
        "sigmask": sorted(int(signum) for signum in signal.pthread_sigmask(signal.SIG_BLOCK, [])),
        **lock(),
    }
    if blocking:
        sleeper = (sys.executable, "-P", "-S", "-c", SLEEP, str(BLOCK))
        entry["grandchild"] = subprocess.Popen(sleeper).pid
    with open(a.log, "a", encoding="utf-8") as log:
        log.write(json.dumps(entry) + "\n")
    if blocking:
        time.sleep(BLOCK)
    codes = dict(rule.split(":") for rule in a.exit)
    return int(codes.get(kind, 0))


def lock() -> dict:
    """The callback lock in the cwd: its text, and whether another open file holds it."""
    try:
        with open(LOCK, encoding="utf-8") as file:
            text = file.read()
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"lock": text, "held": True}
            return {"lock": text, "held": False}
    except FileNotFoundError:
        return {"lock": None, "held": False}


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
