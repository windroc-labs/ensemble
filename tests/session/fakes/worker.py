"""A headless run's worker without a process, scripted as its guard's channel would read."""

import threading
from collections import deque

from ensemble.session.engine.types import Liveness, NotShowable, WorkerExit
from ensemble.session.hosts.base import InteractiveProcess


class ScriptedWorker(InteractiveProcess):
    """read(): each of `batches` in turn, then WorkerExit(code); with code None, its exit (-15)
    comes only once closed, which confirms it. `codes`: returncode()'s answers after the exit, then
    code. Its writes and closes are kept.
    """

    def __init__(self, name="w", generation="g" * 32, batches=(), code=0, codes=()):
        self.name, self.generation = name, generation
        self.batches, self.code, self.codes = deque(batches), code, deque(codes)
        self.exit_read = self.closed = False
        self.writes: list[bytes] = []
        self.closes = 0
        self.condition = threading.Condition()

    def read(self, timeout):
        with self.condition:
            if self.batches:
                return list(self.batches.popleft())
            if self.code is not None and not self.exit_read:
                self.exit_read = True
                return [WorkerExit(self.code)]
            self.condition.wait(min(timeout, 0.05))
            return []

    def write(self, data):
        self.writes.append(data)

    def close(self):
        with self.condition:
            self.closes += 1
            if self.code is None:
                self.code = -15
            self.closed = True
            self.condition.notify_all()

    def state(self):
        return Liveness.ABSENT if self.exit_read or self.closed else Liveness.LIVE

    def returncode(self):
        if not (self.exit_read or self.closed):
            return None
        return self.codes.popleft() if self.codes else self.code

    def send(self, action):
        raise NotShowable(f"worker {self.name}: headless")

    def press(self, keys):
        raise NotShowable(f"worker {self.name}: headless")
