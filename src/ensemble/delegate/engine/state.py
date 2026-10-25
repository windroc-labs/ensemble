"""A Delegate run's state, in its run dir (Layout.run), outside the agent dir it may write.

The run dir holds `delegate.json`, the run record (the run's only state); `delegate.lock`, the
mutex; `delegate.run.lock`, the run lock a running Delegate holds; `delegate.callback.lock`, the
callback lock (its protocol: engine/callback.py). The Delegate makes the run dir before it takes
any lock.
"""

import contextlib
import fcntl
import os
import stat
from collections.abc import Iterator

from ensemble.delegate.engine.types import ConfigError, Layout
from ensemble.utils import file_lock, read_regular, save


class RunState:
    """The run dir's state files for one workdir. Its OSErrors propagate."""

    def __init__(self, layout: Layout) -> None:
        self.layout = layout
        self.record = os.path.join(layout.run, "delegate.json")
        self._lock = os.path.join(layout.run, "delegate")
        self.callback_lock = f"{self._lock}.callback.lock"

    def read(self) -> bytes | None:
        return read_regular(self.record)

    def write(self, payload: str) -> None:
        save(self.record, payload, durable=True)

    @contextlib.contextmanager
    def mutex(self) -> Iterator[None]:
        with file_lock(self._lock):
            yield

    @contextlib.contextmanager
    def run_lock(self) -> Iterator[None]:
        with file_lock(f"{self._lock}.run"):
            yield

    def running(self) -> bool:
        """Whether a run holds the run lock."""
        return self.held(f"{self._lock}.run.lock")

    def held(self, lock: str) -> bool:
        """Whether another open file holds `lock`; a missing one is not."""
        try:
            descriptor = os.open(lock, os.O_RDONLY | os.O_NONBLOCK)
        except FileNotFoundError:
            return False
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                name = os.path.basename(lock).removesuffix(".lock").replace(".", " ")
                raise ConfigError(f"{self.layout.workdir}: invalid {name} lock")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(descriptor)
