"""The delegation handler, registered as HANDLER with state {"workdir", "worker": n} on an owned tui
run: the session harness calls on_event for each event of that worker. Its worker's first call
binds the opening attempt, its TurnStarted after an ending opens a follow-up; the attempt core
decides each event. An ending stores the Result, then notes it, writes the outcome line, closes a
locked run through the owner's view, then sends Finished. Another sid, mode or worker: ignored.
"""

from __future__ import annotations

import threading

from ensemble.delegate.engine.callback import EventCallback
from ensemble.delegate.engine.record import RunRecord, worker
from ensemble.delegate.engine.runner import POLICIES, Attempt, closing, say
from ensemble.delegate.engine.types import (
    Begun,
    ConfigError,
    DelegateState,
    Event,
    Finished,
    Layout,
    Result,
)
from ensemble.session import (
    Blocked,
    EventCall,
    Exited,
    Mode,
    Reply,
    RunLog,
    Session,
    SessionError,
    TurnStarted,
)

HANDLER = "ensemble.delegate.engine.handler"

_runs: dict[tuple[str, str], _Run] = {}
_runs_lock = threading.Lock()


def on_event(call: EventCall) -> None:
    workdir, token = call.state.get("workdir"), call.state.get("worker")
    if not isinstance(workdir, str) or type(token) is not int:
        return
    key = (workdir, call.generation)
    with _runs_lock:
        run = _runs.setdefault(key, _Run(workdir))
    with run.lock:
        if call.event_id in run.handled:
            return
        try:
            run.handle(call, token)
        except (ConfigError, OSError, SessionError) as error:
            say(f"delegate: {error}")
        run.handled.add(call.event_id)
        if isinstance(call.event, Exited):
            run.stop_timer()
            with _runs_lock:
                _runs.pop(key, None)


class _Run:
    """One worker generation's attempts; `deferred`: a locked idle give-up's Finished."""

    def __init__(self, workdir: str) -> None:
        self.record = RunRecord(Layout(workdir))
        self.lock = threading.Lock()
        self.handled: set[str] = set()
        self.n: int | None = None
        self.attempt: Attempt | None = None
        self.state: DelegateState | None = None
        self.session: Session | None = None
        self.callback: EventCallback | None = None
        self.timer: threading.Timer | None = None
        self.ticks = 0
        self.blocked = False
        self.deferred: Result | None = None

    def handle(self, call: EventCall, token: int) -> None:
        event = call.event
        if isinstance(event, Exited) and self.deferred is not None:
            result, self.deferred = self.deferred, None
            self.emit(Finished(result))
        for _ in range(2):
            state = self.record.read()
            plan, n = state.plan, state.attempt
            if call.sid != plan.sid or plan.spec.mode != Mode.TUI or worker(state) != token:
                return
            if state.completed:
                if not isinstance(event, TurnStarted):
                    return
                if self.record.follow_up(n, token):
                    n += 1
                    break
            elif state.bound or self.record.bind(n, token):
                break
        else:
            return
        if self.attempt is None or self.n != n:
            self.begin(self.record.read(), call.session)
        assert self.attempt is not None
        result = self.attempt.feed(event)
        if isinstance(event, Exited) and (
            result is None or result.outcome is None and result.limit is None
        ):
            result = Result(event.returncode, None, f"the client exited {event.returncode}")
        if result is not None:
            self.end(result)
            return
        if isinstance(event, Blocked):
            self.blocked = True
        elif not (self.blocked and isinstance(event, Reply)):
            self.blocked = False
        self.stop_timer()
        if not self.blocked:
            self.start_timer()

    def begin(self, state: DelegateState, session: Session) -> None:
        p = state.plan
        self.n, self.state, self.session, self.blocked = state.attempt, state, session, False
        self.callback = (
            None
            if state.callback is None
            else EventCallback(state.callback, self.record.store, sid=p.sid, attempt=state.attempt)
        )
        note = RunLog(p.session.workdir).note
        self.attempt = Attempt(
            p,
            POLICIES[Mode.TUI](note, None),
            session,
            note=note,
            on_event=self.emit,
            first=not p.spec.resume and state.previous is None,
        )
        self.emit(Begun(p.sid))

    def emit(self, event: Event) -> None:
        if self.callback is not None:
            self.callback(event)

    def start_timer(self) -> None:
        assert self.state is not None
        idle = self.state.plan.run.idle_timeout
        if not idle:
            return
        self.ticks += 1
        tick = self.ticks
        self.timer = threading.Timer(idle, lambda: self.expire(tick, idle))
        self.timer.daemon = True
        self.timer.start()

    def stop_timer(self) -> None:
        self.ticks += 1
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None

    def expire(self, tick: int, idle: float) -> None:
        with self.lock:
            if tick != self.ticks or self.attempt is None:
                return
            try:
                if (result := self.attempt.idle(idle)) is not None:
                    self.end(result, timer=True)
            except (ConfigError, OSError, SessionError) as error:
                say(f"delegate: {error}")

    def end(self, result: Result, *, timer: bool = False) -> None:
        """timer: a locked run's Finished waits for its close's Exited."""
        self.stop_timer()
        state, n, self.attempt = self.state, self.n, None
        assert state is not None and n is not None
        p = state.plan
        if not self.record.complete(n, result, sid=p.sid):
            return
        log = RunLog(p.session.workdir)
        for kind, fields in (
            ("result", closing(result)),
            ("end", {"sid": p.sid, "rc": result.returncode}),
        ):
            try:
                log.note(kind, **fields)
            except OSError as error:
                say(f"delegate: {error}")
        RunRecord.announce(state, result)
        environment = p.session.environment
        lock = None if environment is None else environment.lock
        if lock is not None:
            if timer:
                self.deferred = result
            if self.close(lock, log):
                return
            self.deferred = None
        self.emit(Finished(result))

    def close(self, lock: str, log: RunLog) -> bool:
        assert self.session is not None
        try:
            self.session.close()
        except SessionError as error:
            say(f"delegate: tui: close: {error}")
            return False
        say(f"delegate: session {self.session.name or 'agent'} closed (lock {lock})")
        try:
            log.note("release", lock=lock)
        except OSError as error:
            say(f"delegate: {error}")
        return self.deferred is not None
