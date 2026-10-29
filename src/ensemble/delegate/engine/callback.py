"""Deliver each Delegate event, stamped with its run's sid and attempt, as one stdin line to the
callback command in its own process group. A failure (exit ≠ 0, CALLBACK_TIMEOUT, OSError) is one
unretried `callback_error` row, an exit's with its stderr's tail; an exception once the child
exists kills and reaps the group. The child inherits the held callback lock, which names
`<child pid> <parent pid>`; the parent lock is held while delivering.
"""

import contextlib
import errno
import fcntl
import os
import re
import signal
import stat
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict

from ensemble.delegate.engine.runner import to_stderr
from ensemble.delegate.engine.state import RunState
from ensemble.delegate.engine.types import Begun, ConfigError, Event, Progress
from ensemble.delegate.event import DelegateEvent
from ensemble.session import RunLog
from ensemble.utils import kill_group, one_line

CALLBACK_TIMEOUT = 300  # seconds: longer than the worst-case write-back
CALLBACK_STOP_WAIT = 5  # seconds: a killed group to end, then its lock to free
POLL = 0.05  # seconds
STDERR_TAIL = 2000


class EventCallback:
    def __init__(self, command: Sequence[str], store: RunState, *, sid: str, attempt: int):
        self.command = tuple(command)
        self.store = store
        self.sid, self.attempt = sid, attempt

    def encode(self, event: Event) -> DelegateEvent:
        run = {"sid": self.sid, "attempt": self.attempt}
        if isinstance(event, Begun):
            return DelegateEvent("begun", **run)
        if isinstance(event, Progress):
            return DelegateEvent("progress", **run, name=event.name, text=event.text)
        result = event.result
        return DelegateEvent(
            "finished",
            **run,
            returncode=result.returncode,
            outcome=result.outcome,
            error=result.error,
            limit=None if result.limit is None else asdict(result.limit),
        )

    def __call__(self, event: Event) -> None:
        line = self.encode(event).to_line() + "\n"
        lock, parent_lock = self.store.callback_lock, self.store.callback_parent_lock
        try:
            descriptor = _open(lock)
        except OSError as error:
            self._failed(str(error))
            return
        # locked inside the try that closes it: a signal's Stopped leaves no lock held
        try:
            try:
                _take(descriptor, lock)
                parent = _open(parent_lock)
            except BlockingIOError:
                self._failed(f"callback lock {lock} held: an earlier callback still runs")
                return
            except OSError as error:
                self._failed(str(error))
                return
            try:
                try:
                    _take(parent, parent_lock, wait=True)
                except OSError as error:
                    self._failed(str(error))
                    return
                self._deliver(line, descriptor)
            finally:
                os.close(parent)
        finally:
            os.close(descriptor)

    def _deliver(self, line: str, descriptor: int) -> None:
        child = None
        try:
            try:
                child = subprocess.Popen(
                    self.command,
                    cwd=self.store.layout.run,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    errors="replace",
                    process_group=0,
                    pass_fds=(descriptor,),
                )
            except OSError as error:
                self._failed(str(error))
                return
            with child:
                try:
                    try:
                        os.write(descriptor, f"{child.pid} {os.getpid()}\n".encode())
                    except OSError as error:
                        kill_group(child)
                        self._failed(f"callback lock {self.store.callback_lock}: {error}")
                        return
                    _, stderr = child.communicate(line, timeout=CALLBACK_TIMEOUT)
                except subprocess.TimeoutExpired:
                    kill_group(child)
                    self._failed(f"callback timed out after {CALLBACK_TIMEOUT} s")
                    return
                except BaseException:
                    kill_group(child)
                    raise
            if stderr:
                to_stderr(stderr)
            if child.returncode:
                self._exited(child.returncode, stderr or "")
        except BaseException:
            if child is not None and child.returncode is None:
                kill_group(child)
            raise

    def _exited(self, returncode: int, stderr: str) -> None:
        last = next((one_line(s) for s in reversed(stderr.splitlines()) if s.strip()), "")
        self._failed(
            f"callback exited {returncode}" + (f": {last}" if last else ""),
            stderr=stderr[-STDERR_TAIL:] or None,
        )

    def _failed(self, error: str, *, stderr: str | None = None) -> None:
        log = RunLog(self.store.layout.run)
        if stderr is None:
            log.note("callback_error", error=error, best_effort=True)
        else:
            log.note("callback_error", error=error, stderr=stderr, best_effort=True)


def _open(lock: str) -> int:
    return os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NONBLOCK, 0o600)


def _take(descriptor: int, lock: str, *, wait: bool = False) -> None:
    """Held and emptied; BlockingIOError while held elsewhere (wait: until free)."""
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        raise OSError(errno.EINVAL, "not a regular lock file", lock)
    if wait:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    # a killed group's members hold it until they exit
    elif not _within(lambda: _locked(descriptor)):
        raise BlockingIOError(errno.EWOULDBLOCK, "callback lock held", lock)
    os.ftruncate(descriptor, 0)


def sweep(store: RunState) -> None:
    """Kills the callback group a dead parent left, then awaits its lock; ConfigError while the
    run lock or its parent's lock is held or past CALLBACK_STOP_WAIT.
    """
    if store.running():
        raise ConfigError(f"{store.layout.workdir}: delegate is still alive")
    lock = store.callback_lock
    if not store.held(lock):
        return
    with open(lock, encoding="utf-8", errors="replace") as file:
        text = file.read()
    found = re.fullmatch(r"([0-9]{1,19}) ([0-9]{1,19})\n?", text)
    if not found or any(int(p) <= 1 for p in found.groups()):
        raise ConfigError(f"{lock}: invalid callback pid {text!r}")
    pid, parent = int(found[1]), int(found[2])
    if store.held(store.callback_parent_lock):
        raise ConfigError(f"{lock}: callback still delivered by pid {parent}")
    try:
        if not _gone(pid):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGKILL)
            if not _within(lambda: _gone(pid)):
                raise ConfigError(
                    f"{lock}: callback process group {pid} still exists "
                    f"{CALLBACK_STOP_WAIT} s after SIGKILL"
                )
    except (PermissionError, OverflowError) as error:
        raise ConfigError(f"{lock}: cannot signal callback process group {pid}: {error}") from None
    if not _within(lambda: not store.held(lock)):
        raise ConfigError(
            f"{lock}: callback lock still held {CALLBACK_STOP_WAIT} s after its group ended"
        )


def _locked(descriptor: int) -> bool:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _gone(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return True
    return False


def _within(done: Callable[[], bool]) -> bool:
    deadline = time.monotonic() + CALLBACK_STOP_WAIT
    while not done():
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL)
    return True
