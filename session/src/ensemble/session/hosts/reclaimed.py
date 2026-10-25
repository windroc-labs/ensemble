"""ReclaimedGroup: a process group a Host reclaims by its ProcessControl token."""

from __future__ import annotations

import os
import time

from ensemble.session.engine.types import SessionError
from ensemble.session.hosts.base import Process
from ensemble.utils import Display, ProcessControl, Record

KILL_WAIT = 5
POLL = 0.1


class ReclaimedGroup(Process):
    """Its session may be gone: wait for it, kill it, close it."""

    def __init__(self, control: ProcessControl) -> None:
        self.name = os.path.basename(control.path)
        self._control = control

    @classmethod
    def reclaim(cls, token: str) -> ReclaimedGroup | None:
        """Host.reclaim's group; SessionError if it cannot tell."""
        if not isinstance(token, str):
            return None
        try:
            control = ProcessControl.attach(token)
        except (OSError, ValueError):
            return None
        group = cls(control)
        return group if group.alive() else None

    @property
    def token(self) -> str | None:
        return self._control.path

    def alive(self) -> bool:
        try:
            return self._control.live()
        except OSError as error:
            raise SessionError(f"process group {self.name}: {error}") from error

    def wait_exit(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self.alive():
            if time.monotonic() >= deadline:
                return False
            time.sleep(POLL)
        return True

    def kill(self) -> None:
        try:
            self._control.kill()
        except OSError as error:
            raise SessionError(f"process group {self.name}: {error}") from error
        if not self.wait_exit(KILL_WAIT):
            raise SessionError(f"process group {self.name}: did not exit")

    def close(self) -> None:
        try:
            self._control.terminate()
        except OSError as error:
            raise SessionError(f"process group {self.name}: {error}") from error
        if not self.wait_exit(KILL_WAIT):
            self.kill()

    def record(self) -> Record | None:
        return None

    def display(self) -> Display:
        return None
