"""OwnedSession, the control view of a worker its session harness owns: Session.start and attach
return it in any process. It never owns: closing its events() iterator, dropping it or exiting its
process leaves the worker supervised. Lifecycle (stop, close) goes to the owner as a request; the
rest goes to a read-only attach of the tui session.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence

from ensemble.session.engine.base import Session
from ensemble.session.engine.types import (
    Blocked,
    ClientBound,
    Exited,
    IdleTimeout,
    KeyPress,
    Mode,
    Reply,
    SessionError,
    SessionEvent,
    SessionInfo,
    SessionStatus,
    Stderr,
    TurnEnd,
    Unanswered,
)
from ensemble.session.harness.channel import Channel
from ensemble.session.harness.types import Logged, Op
from ensemble.utils import Display, Record, Status

POLL = 0.5


def fold(
    reply: str | None,
    event: SessionEvent | Stderr | ClientBound,
    *,
    blocked: bool,
    replayed: bool,
) -> tuple[str | None, bool]:
    """What an event, live or replayed, changes: the latest Reply or non-empty TurnEnd text (a
    replayed TurnEnd's even when ""), and blocked from a Blocked until anything but Stderr or Reply.
    """
    if isinstance(event, ClientBound):
        return reply, blocked
    if isinstance(event, Reply):
        reply = event.text
    elif isinstance(event, TurnEnd) and (event.reply or replayed):
        reply = event.reply
    # a client record's Reply can land after the block it preceded
    return reply, isinstance(event, Blocked) or blocked and isinstance(event, Stderr | Reply)


class _Reader:
    """One pass over the log: its records of `generation` with increasing seq, folded."""

    def __init__(self, channel: Channel, generation: str) -> None:
        self.channel, self.generation = channel, generation
        self.cursor, self.seq = 0, 0
        self.reply: str | None = None
        self.blocked = self.exited = False

    def read(self, *, replayed: bool = False) -> list[Logged]:
        found, self.cursor = self.channel.read(self.cursor)
        taken = []
        for logged in found:
            if logged.id != f"{self.generation}:{logged.seq}" or logged.seq <= self.seq:
                continue  # another generation's, or replayed out of order: never acted on
            self.seq = logged.seq
            self.reply, self.blocked = fold(
                self.reply, logged.event, blocked=self.blocked, replayed=replayed
            )
            self.exited = self.exited or isinstance(logged.event, Exited)
            taken.append(logged)
        return taken

    def copy(self) -> _Reader:
        other = _Reader(self.channel, self.generation)
        other.cursor, other.seq, other.reply = self.cursor, self.seq, self.reply
        other.blocked, other.exited = self.blocked, self.exited
        return other


class OwnedSession(Session):
    """attach: the read-only attach of this worker's tui session, bound to its sid and generation;
    replay (attach): starts past the log read once, its records folded into reply.
    """

    def __init__(
        self,
        channel: Channel,
        name: str,
        *,
        client: str,
        sid: str,
        generation: str,
        idle_timeout: float | None,
        attach: Callable[[], Session],
        replay: bool = False,
    ) -> None:
        self.sid, self.client, self.mode, self.name = sid, client, Mode.TUI, name
        self.generation = generation
        self._channel, self._idle, self._attach = channel, idle_timeout, attach
        self._attached: Session | None = None
        self._events = _Reader(channel, generation)
        if replay:
            self._events.read(replayed=True)
        self._latest = self._events.copy()

    def events(self) -> Iterator[SessionEvent | Stderr | IdleTimeout]:
        reader, last = self._events, time.monotonic()
        while not reader.exited:
            taken = reader.read()
            if not taken and not self._channel.alive():
                taken = reader.read()  # what it logged before it let go
                if not taken:
                    raise SessionError(
                        f"session harness {self._channel.directory} ended without the worker's exit"
                    )
            for logged in taken:
                if not isinstance(logged.event, ClientBound):
                    last = time.monotonic()
                    yield logged.event
            if taken:
                continue
            wait = POLL
            if self._idle is not None and not reader.blocked:
                wait = min(POLL, last + self._idle - time.monotonic())
                if wait <= 0:
                    last = time.monotonic()
                    yield IdleTimeout(self._idle)
                    continue
            time.sleep(wait)

    @property
    def reply(self) -> str | None:
        self._latest.read()
        return self._latest.reply

    def stop(self) -> None:
        self._end(Op.STOP)

    def close(self) -> None:
        self._end(Op.CLOSE)

    def _end(self, op: Op) -> None:
        """Its owner's confirmed `op`; with none, the legacy one through the attach, confirmed."""
        if self._channel.request(op):
            return
        try:
            session = self._attach()
        except SessionError:
            return  # bound to this sid and generation: its worker is gone
        if op == Op.STOP:
            session.stop()
        else:
            session.close()
        if session.alive():
            raise Unanswered(f"session {self.name}: still running after {op}")

    def _session(self) -> Session:
        if self._attached is None:
            self._attached = self._attach()
        return self._attached

    @property
    def opening_pending(self) -> str | None:
        return getattr(self._session(), "opening_pending", None)

    def send(self, text: str) -> None:
        self._session().send(text)

    def press(self, keys: Sequence[KeyPress]) -> None:
        self._session().press(keys)

    def alive(self) -> bool:
        try:
            session = self._session()
        except SessionError:
            return False
        return session.alive()

    def status(self) -> Status:
        return self._session().status()

    def content(self) -> str:
        return self._session().content()

    def info(self) -> SessionInfo:
        return self._session().info()

    def session_status(self) -> SessionStatus:
        return self._session().session_status()

    def record(self) -> Record | None:
        return self._session().record()

    @property
    def showable(self) -> bool:
        return self._session().showable

    def show(self, *, display: Display = None, command: str | None = None) -> str | None:
        return self._session().show(display=display, command=command)

    def hide(self) -> None:
        self._session().hide()

    def attach_command(self) -> str:
        return self._session().attach_command()

    def screen(self, lines: int | None = None) -> str:
        return self._session().screen(lines)
