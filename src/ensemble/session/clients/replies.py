"""What the tui clients' mid-turn replies share: a run's locked state file, its reply lines and
the owning harness's settle of it each pump; each client owns its state's shape, scan and hooks.
"""

import contextlib
import hashlib
import json
import os
import sys
import time
from collections.abc import Callable

from ensemble.session.engine.status_log import SessionStatusLog
from ensemble.utils import STALE_AGE, file_lock, read_beneath, save_reply

STATE_MAX = 1 << 20
LOCK_POLL = 0.05
LIFETIME = 6 * 3600  # an open turn's before it is stale
Clock = Callable[[], float]
Sleep = Callable[[float], None]


def load(path: str, fresh: Callable[[], dict], valid: Callable[[object], bool]) -> dict:
    """The state file's state, else fresh()."""
    data = read_beneath(os.path.dirname(path), path, limit=STATE_MAX + 1)
    try:
        state = json.loads(data) if data and len(data) <= STATE_MAX else None
    except (ValueError, RecursionError):
        state = None
    return state if isinstance(state, dict) and valid(state) else fresh()


def directory(path: str) -> None:
    with contextlib.suppress(FileExistsError):
        os.mkdir(path, 0o700)


def prune(folder: str) -> None:
    """Drops state files older than STALE_AGE with their lock files."""
    cutoff = time.time() - STALE_AGE
    with contextlib.suppress(OSError), os.scandir(folder) as entries:
        old = [
            entry.path
            for entry in entries
            if entry.name.endswith(".json")
            and entry.is_file(follow_symlinks=False)
            and entry.stat(follow_symlinks=False).st_mtime < cutoff
        ]
    for path in old:
        for name in (path, path + ".lock"):
            with contextlib.suppress(OSError):
                os.unlink(name)


@contextlib.contextmanager
def locked(path: str, deadline: float, clock: Clock, sleep: Sleep):
    """The state file's lock, waited for until `deadline`, then TimeoutError."""
    while True:
        stack = contextlib.ExitStack()
        try:
            stack.enter_context(file_lock(path, wait=False))
            break
        except BlockingIOError:
            stack.close()
            if clock() >= deadline:
                raise TimeoutError(f"{path}: locked") from None
            sleep(LOCK_POLL)
    with stack:
        yield


def emit(events: str, name: str, key: str, text: str) -> None:
    """Stores the answer once per `key`, then appends its `reply <id>` line."""
    reply_id = hashlib.sha256(key.encode()).hexdigest()[:32]
    if save_reply(events, reply_id, text):
        SessionStatusLog(events).write(name, "reply", reply_id)


def pumped(path: str, settle: Callable[[float], int], label: str) -> None:
    """settle(its deadline, now) of an existing state file, by the owning harness's pump: a held
    lock is left to the next pump, an OSError said on stderr.
    """
    if not os.path.isfile(path):
        return
    try:
        settle(time.monotonic())
    except TimeoutError:
        return
    except OSError as error:
        print(f"{label}: {error}", file=sys.stderr)
