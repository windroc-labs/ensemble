"""The delegation handler, registered as record.HANDLER with state {"workdir", "worker": n} on an owned tui
run: the session harness calls on_event for each event of that worker. Its worker's first call
binds the opening attempt, its TurnStarted after an ending opens a follow-up; the attempt core
decides each event. An ending stores the Result, then notes it, writes the outcome line, closes a
locked run through the owner's view, then sends Finished. Another sid, mode or worker: ignored.
"""

from __future__ import annotations

import threading

from ensemble.delegate.engine.callback import EventCallback
from ensemble.delegate.engine.record import CLOSED, RunRecord, worker
from ensemble.delegate.engine.runner import POLICIES, Attempt, closing, say, stopped
from ensemble.delegate.engine.types import (
    Begun,
    ConfigError,
    DelegateState,
    Event,
    Finished,
    Layout,
    Result,
    Stopped,
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
    TurnFailed,
    TurnStarted,
)

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
        opened = False
        for _ in range(2):
            state = self.record.read()
            plan, n = state.plan, state.attempt
            if call.sid != plan.sid or plan.spec.mode != Mode.TUI or worker(state) != token:
                return
            if state.completed:
                if not isinstance(event, TurnStarted):
                    return
                if self.record.follow_up(n, token):
                    n, opened = n + 1, True
                    break
            elif state.bound or self.record.bind(n, token):
                break
        else:
            return
        if self.attempt is None or self.n != n:
            self.begin(self.record.read(), call.session, opened=opened)
        assert self.attempt is not None
        try:
            result = self.attempt.feed(event)
        except OSError as error:  # a deliverable it could not save: the attempt ends
            say(f"delegate: {error}")
            result = stopped(error)
        if isinstance(event, Exited) and (
            result is None or result.outcome is None and result.limit is None
        ):
            rc = event.returncode
            result = (
                stopped(Stopped(128 - rc))
                if rc < 0
                else Result(rc, None, f"the client exited {rc}")
            )
        if (
            result is not None
            and result.outcome is None
            and result.limit is None
            and (
                isinstance(event, Exited)
                or isinstance(event, TurnFailed)
                and event.error == "exited"
            )
            and self.record.store.closing()
        ):
            result = CLOSED
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

    def begin(self, state: DelegateState, session: Session, *, opened: bool) -> None:
        """opened: a follow-up, whose Begun it sends (an opening one's drive sends its own)."""
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
        if opened:
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
        """Off the Dispatch thread (timer), a locked run's Finished waits for its close's Exited."""
        self.stop_timer()
        state, n, self.attempt = self.state, self.n, None
        assert state is not None and n is not None
        if not self.record.complete(n, result, sid=state.plan.sid):
            return
        if finish(state, result, self.session) and timer:
            self.deferred = result
            return
        self.emit(Finished(result))


def finish(
    state: DelegateState,
    result: Result,
    session: Session | None,
    *,
    give_up: str | None = None,
    notes: bool = True,
) -> bool:
    """A stored ending's notes, outcome line and locked close; whether that close returned."""
    p = state.plan
    log = RunLog(p.session.workdir)
    environment = p.session.environment
    lock = None if environment is None else environment.lock
    rows = [("result", closing(result)), ("end", {"sid": p.sid, "rc": result.returncode})]
    if give_up is not None:
        try:
            command = f": {session.attach_command()}" if session and session.showable else ""
        except SessionError:
            command = ""
        left = "" if lock else f"; session {state.agent_name} left open{command}"
        say(f"delegate: {give_up}{left}")
        rows.insert(0, ("give_up", {"reason": give_up}))
    for kind, fields in rows if notes else ():
        try:
            log.note(kind, **fields)
        except OSError as error:
            say(f"delegate: {error}")
    RunRecord.announce(state, result)
    if lock is None or session is None:
        return False
    try:
        session.close()
    except SessionError as error:
        say(f"delegate: tui: close: {error}")
        return False
    say(f"delegate: session {state.agent_name} closed (lock {lock})")
    try:
        log.note("release", lock=lock)
    except OSError as error:
        say(f"delegate: {error}")
    return True
