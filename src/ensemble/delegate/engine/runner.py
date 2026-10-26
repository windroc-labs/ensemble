"""One role's run to its Result: an invalid outcome gets one fix of the same session, a usage limit
or a failed turn ends the run, the deliverable is saved to `out`; records go to the run dir's
run.jsonl (the session's workdir). A Mode's policy (POLICIES) starts the session, sends the fix and
answers each turn end and IdleTimeout: go on (None), END or GiveUp.
"""

import contextlib
import errno
import os
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

from ensemble.delegate.engine.drivers import Driver
from ensemble.delegate.engine.ready_gate import gate, hosts
from ensemble.delegate.engine.types import (
    Begun,
    ConfigError,
    Event,
    Finished,
    GateStatus,
    Instruction,
    Limit,
    Plan,
    Progress,
    Result,
    Stopped,
)
from ensemble.delegate.event.outcome import SAVES_DELIVERABLE, drop_files
from ensemble.delegate.event.outcome import read as read_outcome
from ensemble.delegate.event.reply import OUTCOME, marks, outcome_block, outcome_only
from ensemble.delegate.event.types import InvalidOutcome, Outcome, Updraft
from ensemble.session import ConfigError as SessionConfigError
from ensemble.session import (
    Exited,
    IdleTimeout,
    LimitHit,
    Mode,
    Reply,
    RunLog,
    Session,
    SessionBusy,
    SessionError,
    Stderr,
    TurnEnd,
    TurnFailed,
)
from ensemble.utils import EXIT_BUSY, holds, printable, save

STOP_LIMIT = 3
FIX = (
    "The outcome block in your last reply is invalid: {error}. Fix it: end your reply with one corrected "
    f"{OUTCOME} block; change nothing else."
)
NUDGE = (
    f"Finish your task, then end your last reply with your outcome block (`{OUTCOME}`), as your instructions' "
    "Return says. If you are waiting for background work, wait for it first."
)
END = "end"


@dataclass(frozen=True)
class GiveUp:
    reason: str


class Headless:
    stops_failed = True  # a failed turn's client would hold its container on

    def __init__(self, note: Callable[..., None], driver: Driver):
        self.note, self.driver = note, driver

    def start(self, p: Plan) -> Session:
        return self._start(p, p.session)

    def _start(self, p: Plan, session) -> Session:
        handle = self.driver.start(
            replace(p.driver_spec, session=session), idle_timeout=p.run.idle_timeout
        )
        if isinstance(handle, Instruction):
            raise ConfigError("session driver returned an Instruction")
        return handle

    def fix(self, handle: Session, p: Plan, text: str) -> Session:
        resumed = self.driver.start(
            replace(p.driver_spec, session=replace(p.session, prompt=text, resume=True)),
            idle_timeout=p.run.idle_timeout,
        )
        if isinstance(resumed, Instruction):
            raise ConfigError("session driver returned an Instruction")
        return resumed

    def turn_end(self, handle: Session, reply: str, pending: int) -> str | GiveUp | None:
        return None

    def progress(self) -> None:
        pass

    def idle(self, seconds: float) -> str | GiveUp | None:
        return None


class Tui(Headless):
    """FR-23: the first turn end without an outcome block gets NUDGE, STOP_LIMIT more give up; a
    progress mark resets that count; a turn end with work pending is ignored. FR-24: an IdleTimeout
    gives up. The session is left open unless locked (_Attempts.release). A showable session
    started at a display is shown as the run's `show` says, unless hidden.
    """

    stops_failed = False

    def __init__(self, note: Callable[..., None], driver: Driver):
        super().__init__(note, driver)
        self.nudged, self.count = False, 0

    def start(self, p: Plan) -> Session:
        handle = self._start(p, replace(p.session, name=p.spec.name, events=p.spec.events))
        name = handle.name
        if name is None:
            raise SessionError("tui session has no name")
        if handle.showable:
            say(f"delegate: tui session {name}: {handle.attach_command()}")
            if self.driver.display is not None and not handle.status().hidden:
                try:
                    handle.show(command=p.run.show if p.run.show not in ("", "none") else None)
                except (SessionConfigError, SessionError) as e:
                    say(f"delegate: show {name}: {e}")
        return handle

    def fix(self, handle: Session, p: Plan, text: str) -> Session:
        handle.send(text)
        return handle

    def turn_end(self, handle: Session, reply: str, pending: int) -> str | GiveUp | None:
        if outcome_block(reply) is not None:
            return END
        if pending:
            return None
        if not self.nudged:
            self.nudged = True
            try:
                handle.send(NUDGE)
            except SessionError as e:
                say(f"delegate: tui: {e}")
            self.note("nudge")
            return None
        self.count += 1
        return (
            GiveUp(f"no outcome after {self.count} turn ends") if self.count >= STOP_LIMIT else None
        )

    def progress(self) -> None:
        self.count = 0

    def idle(self, seconds: float) -> str | GiveUp | None:
        size, unit = (3600, "h") if seconds >= 3600 else (60, "min") if seconds >= 60 else (1, "s")
        return GiveUp(f"no event for {seconds / size:g} {unit}")


POLICIES = {Mode.HEADLESS: Headless, Mode.TUI: Tui, Mode.IN_PROCESS: Headless}


def say(text: str) -> None:
    to_stderr(printable(text) + "\n")


def to_stderr(text: str) -> None:
    """Dropped once stderr is dead: EIO, its terminal hung up; EPIPE, its reader gone. Its fd
    then is /dev/null, else the exit-time flush fails: exit 120.
    """
    try:
        sys.stderr.write(text)
        sys.stderr.flush()
    except OSError as error:
        if error.errno not in (errno.EIO, errno.EPIPE):
            raise
        with contextlib.suppress(OSError, ValueError):
            null = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(null, sys.stderr.fileno())
            finally:
                os.close(null)


def report(result: Result, out: str | None) -> tuple[int, str | None]:
    """(exit code, the valid outcome's status), naming the file `out` when it holds the
    deliverable; None: no file.
    """
    if result.outcome is None:
        say(f"delegate: {result.error}")
        return EXIT_BUSY if SessionBusy.PATTERN.match(result.error) else 1, None
    text = result.outcome.deliverable
    if text and out is not None and holds(path := os.path.abspath(out), text):
        say(f"delegate: deliverable {path}")
    say(f"delegate: status {result.outcome.status}")
    return 0, result.outcome.status


def execute(
    p: Plan,
    *,
    driver: Driver,
    on_event: Callable[[Event], None] | None = None,
    ending: Callable[[BaseException], Result] | None = None,
) -> Result:
    """Run an accepted plan; every returned Result has one final Finished event; `ending` ends
    one that raised (else stopped).
    """
    emit = on_event or (lambda _event: None)
    if not driver.session_based:
        instruction = driver.start(p.driver_spec)
        if isinstance(instruction, Instruction):
            result = Result(0, None, instruction=instruction.path)
            emit(Finished(result))
            return result
        raise ConfigError("instruction driver returned a Session")
    attempts = _Attempts(p, emit, driver)
    try:
        result = attempts.result()
        attempts.release()
        attempts.on_event(Finished(result))
    except BaseException as e:
        try:
            if attempts.handle is not None:
                attempts.stop(attempts.handle)
        except BaseException as failure:
            say(f"delegate: {p.spec.mode}: stop failed: {_named(failure)}")
            raise e from failure  # the run log's, not the stop's
        finally:
            attempts.release()
            if not attempts.refused:
                ended = (ending or stopped)(e)
                attempts.close({"error": ended.error}, ended.returncode)
        raise
    closing = {"error": result.error}
    if result.outcome is not None:
        closing = {
            "outcome": {k: v for k, v in asdict(result.outcome).items() if k != "deliverable"},
            **({"error": result.error} if result.error else {}),
        }
    attempts.close(closing, result.returncode)
    return result


def stopped(error: BaseException) -> Result:
    """How a run that raised `error` ends: code `error.code` for a Stopped or an int-coded
    SystemExit, else 1; error `stopped: <Type>[: text]` on one line.
    """
    code = getattr(error, "code", None)
    return Result(
        code if isinstance(error, (Stopped, SystemExit)) and isinstance(code, int) else 1,
        None,
        f"stopped: {_named(error)}",
    )


def _named(error: BaseException) -> str:
    text = " ".join(str(error).split())
    return type(error).__name__ + (f": {text}" if text else "")


def _resets(at: float | None) -> str:
    if at is None:
        return "reset time unknown"
    try:
        return f"resets at {datetime.fromtimestamp(at).astimezone().isoformat(timespec='seconds')}"
    except (TypeError, ValueError, OverflowError, OSError):
        return "reset time unknown"


class _Attempts:
    def __init__(self, p: Plan, on_event: Callable[[Event], None], driver: Driver):
        self.plan, self.on_event, self.policy = (
            p,
            on_event,
            POLICIES[Mode(p.spec.mode)](self.note, driver),
        )
        self.run_dir = p.session.workdir
        self.seen: set[str] = set()
        self.handle: Session | None = None
        self.refused = False  # the Driver refused the start: nothing to close
        environment = p.session.environment
        self.lock = None if environment is None else environment.lock
        self.released = False

    def note(self, kind: str, **fields) -> None:
        RunLog(self.run_dir).note(kind, **fields)

    def close(self, result: dict, rc: int) -> None:
        """Best effort: an OSError is printed."""
        for kind, fields in (("result", result), ("end", {"sid": self.plan.spec.sid, "rc": rc})):
            try:
                self.note(kind, **fields)
            except OSError as e:
                say(f"delegate: {e}")

    def result(self) -> Result:
        p = self.plan
        try:
            handle = self.policy.start(p)
            self.handle = handle
        except SessionConfigError as e:
            self.refused = True
            raise ConfigError(str(e)) from None
        except SessionError as e:
            self.drop_stale_out()
            self.note("input", text=p.params.input)
            if isinstance(e, SessionBusy):
                return Result(EXIT_BUSY, None, str(e))
            return Result(1, None, f"{p.spec.mode}: {e}")
        stale = self.drop_stale_out()
        self.note("input", text=p.params.input)
        if stale:
            self.stop(handle)
            return Result(1, None, stale)
        self.on_event(Begun(p.sid))
        fixed = False
        while True:
            rc, last, limit, failed = self.attempt(handle)
            diagnostic = f"the client exited {rc}" if rc else ""
            if limit:
                return Result(rc, None, f"usage limit; {_resets(limit.resets_at)}", limit)
            if failed:
                if self.policy.stops_failed:
                    self.stop(handle)
                detail = f" ({failed.detail})" if failed.detail else ""
                return Result(rc, None, f"the client's turn failed: {failed.error}{detail}")
            if (block := outcome_block(last or "")) is None:
                return Result(rc, None, diagnostic or "the agent run returned no outcome")
            if p.docker:
                block, dropped = drop_files(block)
                if dropped:
                    self.note("files_dropped", count=dropped)
            try:
                outcome, updraft = read_outcome(
                    block, p.run.output, p.params.workdir, hosts(p.run.output)
                )
            except InvalidOutcome as e:
                if rc or fixed:
                    return Result(rc, None, diagnostic or f"invalid outcome: {e}")
                self.note("invalid", error=str(e))
                say(f"delegate: invalid outcome: {e}; resuming once")
                try:
                    handle = self.policy.fix(handle, p, FIX.format(error=e))
                    self.handle = handle
                except (SessionConfigError, SessionError) as failed:
                    return Result(1, None, f"invalid outcome: {e}; resume failed: {failed}")
                fixed = True
                continue
            return self.finish(outcome, rc, updraft)

    def release(self) -> None:
        """Closes a locked run's session once, freeing its lock (FR-61); prints a SessionError."""
        if self.lock is None or self.handle is None or self.released:
            return
        self.released = True
        try:
            self.handle.close()
        except SessionError as e:
            say(f"delegate: {self.plan.spec.mode}: close: {e}")
            return
        say(f"delegate: session {self.handle.name or 'agent'} closed (lock {self.lock})")

    def stop(self, handle: Session) -> bool:
        """A SessionError is printed, not raised; False then, the session maybe running."""
        try:
            handle.stop()
        except SessionError as e:
            say(f"delegate: {self.plan.spec.mode}: {e}")
            return False
        return True

    def drop_stale_out(self) -> str:
        """Removes a new run's earlier `out`, only once the Driver took the run; returns why it
        could not, else "".
        """
        s = self.plan.spec
        if (
            s.out and not s.resume and not os.path.isdir(s.out)
        ):  # a resume keeps its session's draft
            try:
                Path(s.out).unlink(missing_ok=True)
            except OSError as e:
                return f"cannot remove the stale out: {e}"
        return ""

    def attempt(self, handle: Session) -> tuple[int, str | None, Limit | None, TurnFailed | None]:
        """rc 0 when the policy or a failed turn ended it, the session running on."""
        rc: int | None = None
        last: str | None = None
        limit: Limit | None = None
        failed: TurnFailed | None = None
        replied: str | None = None
        for event in handle.events():
            answer = None
            if isinstance(event, Reply):
                last = replied = self.reply(event.text)
            elif isinstance(event, TurnEnd):
                if event.reply and event.reply != replied:
                    # A headless turn repeats its Reply at turn end.
                    last = self.reply(event.reply)
                replied = None
                if not limit:
                    answer = self.policy.turn_end(handle, last or "", event.pending)
            elif isinstance(event, Stderr):
                say(event.text)
            elif isinstance(event, IdleTimeout):
                self.note("idle", seconds=event.seconds)
                if not limit:
                    answer = self.policy.idle(event.seconds)
            elif isinstance(event, TurnFailed):
                self.note("turn_failed", error=event.error, detail=event.detail)
                failed = event
                break
            elif isinstance(event, LimitHit):
                self.note("limit", resets_at=event.resets_at)
                limit = Limit(event.resets_at)
                if not self.stop(handle):
                    break
            elif isinstance(event, Exited):
                rc = event.returncode
            if isinstance(answer, GiveUp):
                self.note("give_up", reason=answer.reason)
                name = handle.name
                if name is None:
                    raise SessionError("tui session has no name")
                command = f": {handle.attach_command()}" if handle.showable else ""
                left = "" if self.lock else f"; session {name} left open{command}"
                say(f"delegate: {answer.reason}{left}")
                return 0, None, None, None
            if answer == END:
                break
        return rc or 0, last, limit, failed

    def reply(self, text: str) -> str:
        for line in outcome_only(text).splitlines():
            if line.strip():
                say(line)
        for name, said in marks(text):
            self.note("progress", name=name, text=said)
            say(f"Progress ({name}): {said}")
            self.seen.add(name)
            self.on_event(Progress(name, said))
            self.policy.progress()
        return text

    def finish(self, outcome: Outcome, rc: int, updraft: Updraft | None) -> Result:
        s, output = self.plan.spec, self.plan.run.output
        kind = output["type"]
        if outcome.deliverable and s.out:
            if not holds(s.out, outcome.deliverable):  # else the agent run wrote it there
                try:
                    save(s.out, outcome.deliverable)
                except OSError as e:
                    if kind in SAVES_DELIVERABLE:
                        raise
                    say(f"delegate: cannot save the deliverable to {s.out}: {e}")
                    self.note("save_error", error=str(e))
            if kind == "local":
                outcome = replace(outcome, url=s.out)
        if kind == "pull-request":
            gated = gate(outcome, updraft)
            outcome = gated.outcome
            if gated.status != GateStatus.SKIPPED:
                self.note("ready_gate", status=gated.status, reason=gated.reason)
            if gated.reason:
                say(f"delegate: ready gate: {gated.reason}")
        if not s.resume and outcome.status != "needs_input" and "start" not in self.seen:
            say("delegate: missing progress mark: start")
            self.note("missing", name="start")
        result = Result(rc, outcome, f"the client exited {rc}" if rc else "")
        return result
