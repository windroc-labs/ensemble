"""A test's session harnesses ended before its cleanup removes what they write (docs/tests.md)."""

import contextlib
import fcntl
import os
import threading
import time

from ensemble.session.engine.types import SessionError
from ensemble.session.harness.channel import HEX, Channel
from ensemble.session.harness.types import Op

WAIT = 60  # seconds: past process.DRAIN


def _let_go(channel: Channel, timeout: float) -> bool:
    try:
        fd = os.open(channel.lock, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return True
    taken = threading.Event()

    def take():
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        finally:
            os.close(fd)
            taken.set()

    threading.Thread(target=take, daemon=True).start()
    return taken.wait(timeout)


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
    if running := [
        c.directory for c in live if not _let_go(c, max(0.0, deadline - time.monotonic()))
    ]:
        raise AssertionError(f"session harnesses still running after {wait}s: {running}")
