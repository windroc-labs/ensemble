"""Runs COMMAND with TEXT on its stdin, copying its stdout and stderr; kills it, exiting 124, when
no stdout line is a JSON object with a string sessionID within SECONDS. Unstartable: 127; bad
arguments: 2. Standalone: the standard library only.
"""

import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable

USAGE = "usage: launcher.py --timeout SECONDS --prompt TEXT -- COMMAND..."
STDERR_WAIT = 2  # seconds to copy COMMAND's last stderr after it exits
LOCK_WAIT = 1  # seconds the watch waits for a line being copied


def _arguments(argv: list[str]) -> tuple[float, str, list[str]]:
    if len(argv) < 6 or argv[0] != "--timeout" or argv[2] != "--prompt" or argv[4] != "--":
        raise ValueError
    seconds = float(argv[1])
    if not 0 < seconds < float("inf"):
        raise ValueError
    return seconds, argv[3], argv[5:]


def _bound(line: bytes) -> bool:
    try:
        record = json.loads(line)
    except (ValueError, RecursionError):
        return False
    return isinstance(record, dict) and isinstance(record.get("sessionID"), str)


def _out(data: bytes) -> None:
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _err(data: bytes) -> None:
    sys.stderr.buffer.write(data)
    sys.stderr.buffer.flush()


def _feed(stream, text: str) -> None:
    try:
        with stream:
            stream.write(text.encode())
    except OSError:
        pass


def _copy(stream) -> None:
    try:
        while chunk := stream.read1(65536):
            _err(chunk)
    except (OSError, ValueError):
        pass


class Watch:
    """Copies stdout lines, noting a session; expired without one, kills COMMAND and exits, whatever
    holds its pipes. One lock orders the two.
    """

    def __init__(self, child, seconds: str):
        self.child, self.seconds = child, seconds
        self.lock = threading.Lock()
        self.session = self.ended = False

    def line(self, line: bytes) -> None:
        with self.lock:
            self.session = self.session or _bound(line)
            _out(line)

    def end(self) -> None:
        with self.lock:
            self.ended = True

    def expire(self) -> None:
        # A write stuck on a reader holds the lock: stop all the same unless bound or ended.
        if self.session or self.ended:
            return
        locked = self.lock.acquire(timeout=LOCK_WAIT)
        spared = self.session or self.ended
        if locked:
            self.lock.release()
        if spared:
            return
        self.child.kill()
        _err(f"opencode: no session after {self.seconds} s; stopped\n".encode())
        os._exit(124)


def main(
    argv: list[str],
    *,
    thread: Callable[..., threading.Thread] = threading.Thread,
    timer: Callable[..., threading.Timer] = threading.Timer,
) -> int:
    try:
        seconds, text, command = _arguments(argv)
    except ValueError:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        child = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except FileNotFoundError:
        print(f"opencode: command not found: {command[0]}", file=sys.stderr)
        return 127
    except OSError as error:
        print(f"opencode: cannot run {command[0]}: {error.strerror}", file=sys.stderr)
        return 127
    assert child.stdout is not None
    watch = Watch(child, argv[1])
    expiry = timer(seconds, watch.expire)
    expiry.daemon = True
    copier = thread(target=_copy, args=(child.stderr,), daemon=True)
    for each in (thread(target=_feed, args=(child.stdin, text), daemon=True), copier, expiry):
        each.start()
    try:
        for line in child.stdout:
            watch.line(line)
    except OSError:
        child.kill()
    watch.end()
    code = child.wait()
    expiry.cancel()
    copier.join(STDERR_WAIT)
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
