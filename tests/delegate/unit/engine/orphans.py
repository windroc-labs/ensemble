"""A dead harness's callback, as callback.sweep sees it, without a process: a held callback lock and
an os.killpg fake.
"""

import fcntl
import os
import signal
import unittest


class Orphan:
    """A callback lock `text` held by another open file, and an os.killpg fake over its group: the
    group ends on SIGKILL when `dies` (or is gone once `alive` is false), freeing the lock unless
    `keeps`; a signal in `errors` raises its error.
    """

    def __init__(self, case: unittest.TestCase, lock: str, text: str):
        with open(lock, "w", encoding="utf-8") as file:
            file.write(text)
        self.holder: int | None = os.open(lock, os.O_RDONLY)
        fcntl.flock(self.holder, fcntl.LOCK_EX)
        case.addCleanup(self.free)
        self.calls: list = []
        self.alive, self.dies, self.keeps = True, True, False
        self.errors: dict[int, BaseException] = {}

    def free(self):
        if self.holder is not None:
            os.close(self.holder)
            self.holder = None

    def killpg(self, pid, signum):
        self.calls.append((pid, signum))
        if signum in self.errors:
            raise self.errors[signum]
        if signum == signal.SIGKILL and self.dies:
            self.alive = False
        if not self.alive:
            if not self.keeps:
                self.free()
            if signum == 0:
                raise ProcessLookupError(3, "No such process")
