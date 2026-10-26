"""Crew launch intent, roster entries, states and errors."""

from dataclasses import dataclass
from enum import StrEnum

from ensemble.utils import Record, Status


@dataclass(frozen=True)
class Spec:
    name: str
    argv: tuple[str, ...]
    note: str | None = None


class State(StrEnum):
    STARTING = "starting"
    LIVE = "live"
    DONE = "done"
    WAITING = "waiting"
    GONE = "gone"
    LOST = "lost"
    RESUMING = "resuming"


@dataclass(frozen=True)
class Entry:
    record: Record | None
    state: State
    note: str | None
    started: str
    argv: tuple[str, ...] = ()
    sid: str | None = None
    receipt: str | None = None


@dataclass(frozen=True)
class Lease:
    id: str
    generation: int


class RosterError(Exception):
    pass


class StartRefused(ValueError):
    pass


class StartBusy(RuntimeError):
    """A role start exited EXIT_BUSY before printing anything: its lock is held, nothing started."""


class Stale(RosterError):
    pass


class StaleHolder(Stale):
    """The caller's holder pair names a holder that ended or was replaced."""


class Held(RosterError):
    pass


class StatusError(RuntimeError):
    """Some keys' status failed, each named in the message; statuses: the other keys' answers."""

    def __init__(self, message: str, statuses: list[Status]):
        super().__init__(message)
        self.statuses = statuses
