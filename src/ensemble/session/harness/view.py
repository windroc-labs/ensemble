"""OwnedSession, the control view of an owned worker: never its owner. stop and close are
requests, and so are a remote worker's send, press and screen; the rest goes to a read-only attach.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import TypeVar

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
    StatusState,
    Stderr,
    TurnEnd,
    Unanswered,
)
from ensemble.session.harness.channel import Channel, Tie
from ensemble.session.harness.types import Logged, Op
from ensemble.utils import Display, Record, Status

POLL = 0.1
T = TypeVar("T")


def fold(
    reply: str | None,
    event: SessionEvent | Stderr | ClientBound,
    *,
    blocked: bool,
    replayed: bool,
) -> tuple[str | None, bool]:
    """reply: the latest Reply or non-empty TurnEnd text (replayed: even ""); blocked from a
    Blocked until anything but Stderr or Reply.
    """
    if isinstance(event, ClientBound):
        return reply, blocked
    if isinstance(event, Reply):
        reply = event.text
    elif isinstance(event, TurnEnd) and (event.reply or replayed):
        reply = event.reply
    # a Reply can land after the block it preceded
    return reply, isinstance(event, Blocked) or blocked and isinstance(event, Stderr | Reply)


class _Reader:
    """The log's records of `generation` with increasing seq, folded."""

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
                continue  # stale or forged
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
    """attach: the read-only attach, bound to sid and generation (attached: one made already);
    retire: a tui view's non-owner close of that generation; replay: start past the log. A headless
    view's events() stops its worker on an interrupt, as a headless HostedSession does; tie: a
    headless start's caller pipe, its harness closing the worker once the view is gone.
    """

    def __init__(
        self,
        channel: Channel,
        name: str | None,
        *,
        client: str,
        sid: str,
        generation: str,
        idle_timeout: float | None,
        attach: Callable[[], Session],
        attached: Session | None = None,
        retire: Callable[[], None] | None = None,
        replay: bool = False,
        mode: Mode = Mode.TUI,
        tie: Tie | None = None,
    ) -> None:
        if mode == Mode.TUI and retire is None:
            raise ValueError("retire: required for a tui view")
        self.sid, self.client, self.mode, self.name = sid, client, mode, name
        self._tie = tie
        self.generation = generation
        self._channel, self._idle, self._attach = channel, idle_timeout, attach
        self._retire = retire
        self._attached = attached
        self._lock = threading.Lock()  # over _attached, never over a call on it
        self._events = _Reader(channel, generation)
        if replay:
            self._events.read(replayed=True)
        self._latest = self._events.copy()

    def events(self) -> Iterator[SessionEvent | Stderr | IdleTimeout]:
        try:
            yield from self._read()
        except GeneratorExit:  # the caller closed this iterator; the worker goes on
            raise
        except BaseException as error:
            if self.mode != Mode.TUI:
                try:
                    self.stop()
                except SessionError as failed:
                    error.add_note(f"stop: {failed}")
            raise

    def _read(self) -> Iterator[SessionEvent | Stderr | IdleTimeout]:
        reader, last = self._events, time.monotonic()
        while not reader.exited:
            taken = reader.read()
            if not taken and not self._channel.alive():
                taken = reader.read()  # logged before it let go
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
        if self._tie is not None:  # its run is over
            self._tie.close()

    @property
    def reply(self) -> str | None:
        self._latest.read()
        return self._latest.reply

    def stop(self) -> None:
        self._end(Op.STOP)

    def close(self) -> None:
        self._end(Op.CLOSE)

    def _end(self, op: Op) -> None:
        """With no owner: a tui view retires, its generation only (the record rule), for either
        op; a headless one ends through the attach, confirmed.
        """
        if self._channel.request(op):
            return
        if self._retire is not None and self.mode == Mode.TUI:
            self._retire()
            return
        try:
            session = self._attach()
        except SessionError:
            return  # its worker is gone
        if op == Op.STOP:
            session.stop()
        else:
            session.close()
        if session.alive():
            raise Unanswered(f"session {self.name or self.sid}: still running after {op}")

    def _held(self) -> tuple[Session, bool]:
        """Its attach, and whether it was held already."""
        with self._lock:
            if self._attached is not None:
                return self._attached, True
            self._attached = self._attach()
            return self._attached, False

    def _session(self) -> Session:
        return self._held()[0]

    def _drop(self, session: Session) -> None:
        with self._lock:
            if self._attached is session:
                self._attached = None

    def _call(self, call: Callable[[Session], T]) -> T:
        """call on its attach; a held one's SessionError (its display reopened since, say) drops it
        for one call on a new attach. Unanswered (a partly sent send, say) is no proof the call
        failed: raised.
        """
        session, held = self._held()
        try:
            return call(session)
        except Unanswered:
            raise
        except SessionError:
            if not held:
                raise
            self._drop(session)
        return call(self._session())

    @property
    def opening_pending(self) -> str | None:
        opening = self._channel.status().get("opening")
        return opening if isinstance(opening, str) else None

    @property
    def client_up(self) -> bool:
        """Its harness says its guard reported its client up (harness.json `up`)."""
        return self._channel.status().get("up") is True

    def _remote(self) -> bool:
        return self._channel.status().get("remote") is True

    def _ask(self, op: Op, data: Mapping) -> dict:
        if not self._channel.alive():
            raise SessionError(f"session {self.name}: its harness is gone")
        try:
            return self._channel.ask(op, data)
        except SessionError as error:
            if not self._channel.alive() and not isinstance(error, Unanswered):
                raise SessionError(f"session {self.name}: its harness is gone") from None
            raise

    def send(self, text: str) -> None:
        if self._remote():
            self._ask(Op.SEND, {"text": text})
            return
        self._call(lambda session: session.send(text))

    def press(self, keys: Sequence[KeyPress]) -> None:
        if self._remote():
            self._ask(Op.PRESS, {"keys": [str(key) for key in keys]})
            return
        self._call(lambda session: session.press(keys))

    def alive(self) -> bool:
        """A held attach that says not alive is asked once more of a new one."""
        try:
            session, held = self._held()
            if session.alive():
                return True
            if not held:
                return False
            self._drop(session)
            return self._session().alive()
        except SessionError:
            return False

    def status(self) -> Status:
        return self._call(lambda session: session.status())

    def content(self) -> str:
        if not self._remote():
            return self._call(lambda session: session.content())
        if self.status().state == StatusState.DONE and (reply := self.reply):
            return reply
        return self.screen()

    def info(self) -> SessionInfo:
        return self._call(lambda session: session.info())

    def session_status(self) -> SessionStatus:
        return self._call(lambda session: session.session_status())

    def record(self) -> Record | None:
        return self._call(lambda session: session.record())

    @property
    def showable(self) -> bool:
        return self._call(lambda session: session.showable)

    def show(self, *, display: Display = None, command: str | None = None) -> str | None:
        return self._call(lambda session: session.show(display=display, command=command))

    def hide(self) -> None:
        self._call(lambda session: session.hide())

    def attach_command(self) -> str:
        return self._call(lambda session: session.attach_command())

    def screen(self, lines: int | None = None) -> str:
        if self._remote():
            text = self._ask(Op.SCREEN, {"lines": lines}).get("text")
            if not isinstance(text, str):
                raise SessionError(f"session {self.name}: bad screen from its harness")
            return text
        return self._call(lambda session: session.screen(lines))
