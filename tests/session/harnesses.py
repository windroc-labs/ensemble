"""A test's session harnesses ended before its cleanup removes what they write (docs/tests.md)."""

import contextlib
import fcntl
import os
import signal
import time

from ensemble.session.engine.hosted_session import HARNESS
from ensemble.session.engine.types import SessionError
from ensemble.session.harness import process
from ensemble.session.harness.channel import HEX, SLICE, Channel
from ensemble.session.harness.types import Op

WAIT = 60  # seconds: past process.DRAIN
KILL_WAIT = 10  # seconds: FR-13's reap bound


def _let_go(channel: Channel, timeout: float) -> bool:
    """Polled here, so nothing left waiting can take the lock once this returns."""
    try:
        fd = os.open(channel.lock, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return True
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(SLICE)
            else:
                return True
    finally:
        os.close(fd)


def end(home: str, wait: float = WAIT) -> None:
    """Close each live harness under home and wait for it to let go of its lock."""
    root = os.path.join(home, ".ensemble", "harness")
    try:
        names = sorted(os.listdir(root))
    except (FileNotFoundError, NotADirectoryError):
        return
    live = [
        c for c in (Channel(os.path.join(root, n)) for n in names if HEX.fullmatch(n)) if c.alive()
    ]
    deadline = time.monotonic() + wait
    for channel in live:
        with contextlib.suppress(SessionError):  # the wait below decides
            channel.request(Op.CLOSE, timeout=max(0.0, deadline - time.monotonic()))
    if stuck := [c for c in live if not _let_go(c, max(0.0, deadline - time.monotonic()))]:
        for channel in stuck:
            _signal(channel)
        running = [c.directory for c in stuck]
        raise AssertionError(
            f"session harnesses still running after {wait}s, then signalled: {running}"
        )


def _signallable(pid: object) -> bool:
    return type(pid) is int and pid > 1 and pid != os.getpid()


def _signal(channel: Channel) -> None:
    """SIGTERM, then SIGKILL, each only while its lock is held."""
    pid = channel.status().get("pid")
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not _signallable(pid) or not channel.alive():
            return
        try:
            os.kill(pid, sig)
        except (OSError, OverflowError):
            return
        if _let_go(channel, KILL_WAIT):
            return


def spawned(argv) -> bool:
    return HARNESS in argv


class Inline:
    """A spawned harness run here, so fakes answer: a failing start."""

    def __init__(self, argv) -> None:
        channel = Channel(argv[-1])
        channel.hold()
        try:
            self.returncode = process.Harness(channel).run(process._begin(channel.directory))
        finally:
            channel.release()
        self.pid = os.getpid()

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def terminate(self) -> None:
        pass

    kill = terminate
