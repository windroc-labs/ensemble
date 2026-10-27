"""A test's session harnesses ended before it removes what they write: their HOME's harness dirs,
a handler's records. A harness outlives the start that made it (a lockless tui's until its worker
ends) and writes after its close reply, so a test registers end() after creating those dirs.
"""

import contextlib
import os
import time

from ensemble.session.engine.types import SessionError
from ensemble.session.harness.channel import HEX, Channel
from ensemble.session.harness.types import Op

WAIT = 60  # seconds: past process.DRAIN, a harness's wait for its handlers
SLICE = 0.05


def end(home: str, wait: float = WAIT) -> None:
    """Close each session harness under home still holding its lock, then wait for each to let go
    of it; AssertionError naming those still running past wait.
    """
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
        with contextlib.suppress(SessionError):  # refused or unanswered: the wait below decides
            channel.request(Op.CLOSE, timeout=max(0.0, deadline - time.monotonic()))
    while live := [c for c in live if c.alive()]:
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"session harnesses still running after {wait}s: {[c.directory for c in live]}"
            )
        time.sleep(SLICE)
