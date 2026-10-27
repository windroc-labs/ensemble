"""OwnedSession, the control view of an owned worker: never its owner. stop and close are
requests, and so are a remote worker's send, press and screen; the rest goes to a read-only attach.
OwnedInProcess: an in_process worker's, no attach.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping, Sequence

from ensemble.session.engine.base import Session
from ensemble.session.engine.types import (
    Blocked,
    ClientBound,
    Exited,
    IdleTimeout,
    KeyPress,
    Mode,
    NotShowable,
    Reply,
    Resumable,
    SessionError,
    SessionEvent,
    SessionInfo,
    SessionStatus,
    StatusState,
    Stderr,
    TurnEnd,
    Unanswered,
)
from ensemble.session.harness.channel import Channel
from ensemble.session.harness.types import Logged, Op
from ensemble.utils import Display, Record, Status

POLL = 0.1


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
    """attach: the read-only attach, bound to sid and generation; retire: the non-owner close of
    that generation; replay: start past the log.
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
        retire: Callable[[], None],
        replay: bool = False,
    ) -> None:
        self.sid, self.client, self.mode, self.name = sid, client, Mode.TUI, name
        self.generation = generation
        self._channel, self._idle, self._attach = channel, idle_timeout, attach
        self._retire = retire
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

    @property
    def reply(self) -> str | None:
        self._latest.read()
        return self._latest.reply

    def stop(self) -> None:
        self._end(Op.STOP)

    def close(self) -> None:
        self._end(Op.CLOSE)

    def _end(self, op: Op) -> None:
        """With no owner: retire, its generation only (the record rule), for either op."""
        if not self._channel.request(op):
            self._retire()

    def _session(self) -> Session:
        if self._attached is None:
            self._attached = self._attach()
        return self._attached

    @property
    def opening_pending(self) -> str | None:
        opening = self._channel.status().get("opening")
        return opening if isinstance(opening, str) else None

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
        self._session().send(text)

    def press(self, keys: Sequence[KeyPress]) -> None:
        if self._remote():
            self._ask(Op.PRESS, {"keys": [str(key) for key in keys]})
            return
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
        if not self._remote():
            return self._session().content()
        if self.status().state == StatusState.DONE and (reply := self.reply):
            return reply
        return self.screen()

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
        if self._remote():
            text = self._ask(Op.SCREEN, {"lines": lines}).get("text")
            if not isinstance(text, str):
                raise SessionError(f"session {self.name}: bad screen from its harness")
            return text
        return self._session().screen(lines)


class OwnedInProcess(OwnedSession):
    """alive: its harness holds on and has not logged Exited; info: its start's."""

    def __init__(
        self,
        channel: Channel,
        *,
        client: str,
        sid: str,
        generation: str,
        idle_timeout: float | None,
        info: SessionInfo,
        replay: bool = False,
    ) -> None:
        super().__init__(
            channel,
            sid,
            client=client,
            sid=sid,
            generation=generation,
            idle_timeout=idle_timeout,
            attach=self._unattached,
            retire=lambda: None,  # ended with its harness
            replay=replay,
        )
        self.mode, self.name, self._info = Mode.IN_PROCESS, None, info

    @staticmethod
    def _unattached() -> Session:
        raise SessionError("an in_process worker has no attach")

    def send(self, text: str) -> None:
        raise SessionError("send: tui mode only")

    def press(self, keys: Sequence[KeyPress]) -> None:
        raise SessionError("press: tui mode only")

    def screen(self, lines: int | None = None) -> str:
        raise SessionError("screen: tui mode only")

    def alive(self) -> bool:
        self._latest.read()
        return self._channel.alive() and not self._latest.exited

    def status(self) -> Status:
        alive = self.alive()
        return Status(
            name=self.sid,
            alive=alive,
            state=StatusState.WORKING if alive else StatusState.DEAD,
            outcome=None,
            attach=None,
            hidden=False,
            cwd=self._info.cwd if alive else None,
        )

    def content(self) -> str:
        return self.reply or ""

    def info(self) -> SessionInfo:
        return self._info

    def session_status(self) -> SessionStatus:
        return SessionStatus(Resumable.NO, None, "")

    def record(self) -> Record | None:
        return None

    @property
    def showable(self) -> bool:
        return False

    def show(self, *, display: Display = None, command: str | None = None) -> str | None:
        raise NotShowable("show: tui mode only")

    def hide(self) -> None:
        raise NotShowable("hide: tui mode only")

    def attach_command(self) -> str:
        raise NotShowable("attach_command: tui mode only")
