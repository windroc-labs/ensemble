"""What the tui clients' mid-turn replies share: a run's locked state file, its reply lines and
its detached watcher; each client owns its state's shape, scan and hooks.
"""

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable

import ensemble.utils
from ensemble.session.engine.status_log import SessionStatusLog
from ensemble.utils import (
    STALE_AGE,
    file_lock,
    module_roots,
    read_beneath,
    save_reply,
    source_path,
)

STATE_MAX = 1 << 20
LOCK_POLL = 0.05
WATCH_LOCK_WAIT = 1.0
POLL = 1.0
IDLE_EXIT = 2.0
LIFETIME = 6 * 3600  # a watcher's, and an open turn's before it is stale
QUIET = 1800  # seconds the session file may stay unchanged before a watcher ends
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
        for name in (path, path + ".lock", path + ".watch.lock"):
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


def ensure_watcher(path: str, module: str) -> None:
    """Starts `module` (python -P -s -m) watching `path` unless a watcher holds its lock."""
    try:
        with file_lock(path + ".watch", wait=False):
            pass
    except BlockingIOError:
        return
    subprocess.Popen(  # detached: never waited for
        [sys.executable, "-P", "-s", "-m", module, path],
        start_new_session=True,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, **source_path(module_roots(sys.modules[__name__], ensemble.utils))},
        cwd="/",
    )


def watch(
    path: str,
    settle: Callable[[float], int],
    size: Callable[[], int],
    *,
    clock: Clock,
    sleep: Sleep,
    label: str,
) -> int:
    """settle(lock deadline) -> pending answers, every POLL s; ends IDLE_EXIT s after none,
    once size() stays for QUIET s, after LIFETIME s, or at once while another watcher runs.
    """
    try:
        with file_lock(path + ".watch", wait=False):
            return _watch(settle, size, clock, sleep, label)
    except BlockingIOError:
        return 0


def main(argv: list[str], run: Callable[[str], int]) -> int:
    """A watcher module's entry: run(its absolute state file)."""
    if len(argv) != 1 or not argv[0].startswith("/"):
        print("usage: watch <absolute state file>", file=sys.stderr)
        return 2
    return run(argv[0])


def _watch(settle, size, clock: Clock, sleep: Sleep, label: str) -> int:
    began = idle = grew = clock()
    last = -1
    while clock() - began < LIFETIME:
        try:
            pending = settle(clock() + WATCH_LOCK_WAIT)
        except (TimeoutError, OSError) as error:
            print(f"{label}: {error}", file=sys.stderr)
            pending = 1
        if pending:
            idle = clock()
        elif clock() - idle >= IDLE_EXIT:
            return 0
        found = size()
        if found != last:
            last, grew = found, clock()
        elif clock() - grew >= QUIET:
            return 0
        sleep(POLL)
    return 0
