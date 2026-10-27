"""The session harness, `python3 -P -s -m ensemble.session.harness.process [--caller FD] <dir>`:
owns one worker from start to close (docs/sessions.md › Session harness). SIGTERM, SIGHUP and EOF
on FD (a headless start's caller pipe, channel.Tie) close.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from ensemble.session.engine.hosted_session import HostedSession
from ensemble.session.engine.types import (
    ClientBound,
    ConfigError,
    Exited,
    SessionBusy,
    SessionError,
    SessionEvent,
    Stderr,
    TurnEnd,
    TurnFailed,
    TurnStarted,
)
from ensemble.session.harness.channel import SLICE, Channel, encode, hung_up
from ensemble.session.harness.dispatch import Dispatch, load_handlers
from ensemble.session.harness.types import Logged, Op

POLL = 0.5
CLOSE_WAIT = 10
DRAIN = 30
STATUS_EVERY = 1.0
LOG = logging.getLogger("session")

Event = SessionEvent | Stderr | ClientBound


class Worker(Protocol):
    """hosted_session.Owned."""

    @property
    def name(self) -> str | None: ...
    @property
    def sid(self) -> str: ...
    @property
    def generation(self) -> str: ...
    @property
    def opening_pending(self) -> str | None: ...

    def pump(self, wait: float) -> tuple[list[Event], bool]: ...
    def record(self, event: Event) -> None: ...
    def stop(self) -> list[Event]: ...
    def close(self) -> list[Event]: ...
    def returncode(self) -> int | None: ...
    def confirm_end(self, timeout: float) -> int | None: ...
    def view(self, channel: Channel, idle_timeout: float | None = None) -> Any: ...


Begin = Callable[[dict], tuple[Worker, Sequence[Any], Sequence[Mapping[str, object]]]]


class Turns:
    """Turn ids <generation>:t<n>; a TurnStarted fails an open one as superseded."""

    def __init__(self, generation: str) -> None:
        self._generation, self._n = generation, 0
        self._open: str | None = None

    def _next(self) -> str:
        self._n += 1
        return f"{self._generation}:t{self._n}"

    def assign(self, event: Event) -> list[tuple[str | None, Event]]:
        if isinstance(event, TurnStarted):
            found = self._fail("superseded", "")
            self._open = self._next()
            return [*found, (self._open, event)]
        if isinstance(event, TurnEnd | TurnFailed):
            turn = self._open or self._next()
            self._open = None
            return [(turn, event)]
        return [(self._open, event)]

    def exited(self, code: int) -> list[tuple[str | None, Event]]:
        return self._fail("exited", f"worker exited {code} mid-turn")

    def _fail(self, error: str, detail: str) -> list[tuple[str | None, Event]]:
        if self._open is None:
            return []
        turn, self._open = self._open, None
        return [(turn, TurnFailed(error, detail))]


class Harness:
    """The owner loop; its channel already held."""

    def __init__(
        self,
        channel: Channel,
        *,
        poll: float = POLL,
        close_wait: float = CLOSE_WAIT,
        drain: float = DRAIN,
        caller: int | None = None,
    ) -> None:
        """caller: the far end of its caller's pipe, if any."""
        self._channel, self._poll, self._close_wait, self._drain = channel, poll, close_wait, drain
        self._caller = caller
        self._status: dict[str, Any] = {"state": "starting", "pid": os.getpid(), "pending": 0}
        self._seq, self._written = 0, 0.0
        self._signal: int | None = None
        self._worker: Worker | None = None
        self._dispatch: Dispatch | None = None
        self._turns = Turns("")
        self._leftover: list[tuple[str, Op]] = []

    def signalled(self, number: int, _frame: object) -> None:
        self._signal = number

    def run(self, begin: Begin) -> int:
        """0 once the worker ended and handlers drained; 1 for a failed start or loop."""
        self._write()
        try:
            request = self._channel.take_request()
            worker, handlers, states = begin(request)
        except (ConfigError, SessionError) as error:
            self._channel.discard_request()
            return self._failed(error)
        self._worker, self._turns = worker, Turns(worker.generation)
        self._dispatch = Dispatch(
            handlers,
            states,
            worker.view(self._channel),
            self._channel,
            sid=worker.sid,
            generation=worker.generation,
        )
        self._status.update(
            state="running", sid=worker.sid, name=worker.name, generation=worker.generation
        )
        self._write()
        try:
            code = self._supervise(worker)
        except Exception:  # end the worker, then fail
            LOG.exception("session harness %s: failed", self._channel.directory)
            return self._abandon(worker)
        self._exited(code)
        self._drain_handlers(worker)
        return 0

    def _supervise(self, worker: Worker) -> int:
        while True:
            batch = self._channel.requests()
            for index, (ident, op) in enumerate(batch):
                code = self._end(worker, op, ident)
                if code is not None:
                    self._leftover = batch[index + 1 :]  # taken already: served after the exit
                    return code
            if self._caller is not None and hung_up(self._caller):
                LOG.info("session harness %s: its caller ended", self._channel.directory)
                self._caller, self._signal = None, signal.SIGHUP  # closed once, as for a signal
            if self._signal is not None:
                code = self._end(worker, Op.CLOSE, None)
                if code is not None:
                    return code
                self._signal = None
            found, ended = worker.pump(self._poll)
            self._log(found)
            if (opening := worker.opening_pending) != self._status.get("opening"):
                self._status["opening"] = opening
                self._write()
            self._write_pending()
            if ended:
                code = worker.returncode()
                if code is None:  # a stopped headless run's may land later
                    code = worker.confirm_end(self._close_wait)
                return -signal.SIGHUP if code is None else code

    def _end(self, worker: Worker, op: Op, ident: str | None) -> int | None:
        """The exit code once confirmed (Exited logged, then answered); else None, answered."""
        try:
            final = worker.stop() if op == Op.STOP else worker.close()
        except SessionError as error:
            self._answer(ident, ok=False, error=str(error))
            return None
        self._log(final)
        code = worker.confirm_end(self._close_wait)
        if code is None:
            self._answer(
                ident,
                ok=False,
                error=f"{worker.name or worker.sid}: worker exit not confirmed",
                unanswered=True,
            )
            return None
        self._exited(code)
        self._answer(ident, ok=True)
        return code

    def _exited(self, code: int) -> None:
        if self._status["state"] == "exited":
            return
        for turn, event in self._turns.exited(code):
            self._append(turn, event)
        self._append(None, Exited(code))
        self._status.update(state="exited", returncode=code)
        self._write_pending(force=True)

    def _drain_handlers(self, worker: Worker) -> None:
        assert self._dispatch is not None
        deadline = time.monotonic() + self._drain
        while self._dispatch.drain(SLICE) and time.monotonic() < deadline:
            self._serve_ended(worker)
        self._serve_ended(worker)
        self._write_pending(force=True)
        self._dispatch.stop()

    def _serve_ended(self, worker: Worker) -> None:
        """After the exit a request ends what is left: terminal, tui dir, viewer."""
        batch, self._leftover = [*self._leftover, *self._channel.requests()], []
        for ident, op in batch:
            try:
                if op == Op.STOP:
                    worker.stop()
                else:
                    worker.close()
            except SessionError as error:
                self._answer(ident, ok=False, error=str(error))
            else:
                self._answer(ident, ok=True)

    def _abandon(self, worker: Worker) -> int:
        try:
            self._log(worker.close())
            code = worker.confirm_end(self._close_wait)
        except Exception:  # still fail
            LOG.exception("session harness %s: close", self._channel.directory)
            code = None
        if code is not None:
            self._exited(code)
        self._status["state"] = "failed"
        self._write_pending(force=True)
        if self._dispatch is not None:
            self._dispatch.stop()
        return 1

    def _failed(self, error: SessionError | ConfigError) -> int:
        self._status.update(
            state="failed", error={"type": type(error).__name__, "message": str(error)}
        )
        if isinstance(error, SessionBusy):
            self._status["error"].update(lock=error.lock, holder=error.holder)
        self._write()
        return 1

    def _log(self, events: Sequence[Event]) -> None:
        assert self._worker is not None
        for event in events:
            self._worker.record(event)
            for turn, assigned in self._turns.assign(event):
                self._append(turn, assigned)

    def _append(self, turn: str | None, event: Event) -> None:
        assert self._worker is not None and self._dispatch is not None
        self._seq += 1
        logged = Logged(self._seq, f"{self._worker.generation}:{self._seq}", turn, event)
        self._channel.append({"seq": logged.seq, "id": logged.id, "turn": turn, **encode(event)})
        self._dispatch.put(logged)

    def _answer(self, ident: str | None, *, ok: bool, error: str | None = None, **kw) -> None:
        if ident is not None:
            self._channel.reply(ident, ok=ok, error=error, **kw)

    def _write_pending(self, *, force: bool = False) -> None:
        pending = 0 if self._dispatch is None else self._dispatch.pending()
        now = time.monotonic()
        if force or pending != self._status["pending"] and now - self._written >= STATUS_EVERY:
            self._status["pending"] = pending
            self._write()

    def _write(self) -> None:
        self._written = time.monotonic()
        try:
            self._channel.write_status(self._status)
        except OSError as error:
            LOG.warning("session harness %s: harness.json: %s", self._channel.directory, error)


def _begin(directory: str, session: type[HostedSession] = HostedSession) -> Begin:
    def begin(request: dict):
        spec, display, host, roots, pythonpath = session.from_harness_request(request)
        modules = load_handlers(spec.handlers, roots, load=True)
        if pythonpath is None:  # the worker's, as the caller's; its own imports are done
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = pythonpath
        worker = session.own(spec, display=display, host=host, owner=directory)
        return worker, modules, [handler.state for handler in spec.handlers]

    return begin


def main(argv: Sequence[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    caller = _caller(argv[1:-1])
    if len(argv) not in (2, 4) or caller == -1:
        print(
            "usage: python3 -m ensemble.session.harness.process [--caller FD] HARNESS_DIR",
            file=sys.stderr,
        )
        return 2
    channel = Channel(argv[-1])
    try:
        channel.hold()
    except (OSError, SessionError) as error:
        print(f"session harness {argv[-1]}: {error}", file=sys.stderr)
        return 1
    harness = Harness(channel, caller=caller)
    for number in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, harness.signalled)
    try:
        return harness.run(_begin(channel.directory))
    finally:
        channel.release()


def _caller(option: Sequence[str]) -> int | None:
    """`--caller FD`'s FD, None without it, -1 when bad."""
    if not option:
        return None
    if len(option) != 2 or option[0] != "--caller" or not option[1].isdigit():
        return -1
    return int(option[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
