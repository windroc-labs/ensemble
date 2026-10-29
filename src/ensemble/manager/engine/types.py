"""Crew launch intent, roster entries, states, managers as listed and errors."""

from dataclasses import dataclass
from enum import StrEnum
from typing import NamedTuple

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


class Liveness(NamedTuple):
    """Names that answered alive, and those whose liveness is unknown."""

    live: list[str]
    unknown: list[str]


@dataclass(frozen=True)
class Unread:
    """A file that could not be read, and why."""

    error: str


class HolderState(StrEnum):
    CLIENT_RUNNING = "client-running"
    HOLD_ONLY = "hold-only"
    NOT_HELD = "not-held"


@dataclass(frozen=True)
class SessionRecord:
    """A manager's session.json: the client that last started, resumed, cleared or compacted."""

    client: str
    sid: str
    pid: int
    cwd: str


@dataclass(frozen=True)
class Manager:
    """One manager read without a lease; holder: its active holder's label, None when NOT_HELD."""

    name: str
    state: HolderState
    holder: str | None
    record: SessionRecord | Unread | None
    entries: dict[str, Entry] | Unread

    def __post_init__(self):
        if (self.state == HolderState.NOT_HELD) != (self.holder is None):
            raise ValueError("holder: want a label exactly when held")


@dataclass(frozen=True)
class Unreadable:
    """A manager whose holder probe could not run, so nothing about it is known."""

    name: str
    error: str


@dataclass(frozen=True)
class Lease:
    id: str
    generation: int


class RosterError(Exception):
    pass


class StartRefused(ValueError):
    pass


class StartBusy(RuntimeError):
    """A run/role starter exited EXIT_BUSY with no output: its lock is held, nothing started."""


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
