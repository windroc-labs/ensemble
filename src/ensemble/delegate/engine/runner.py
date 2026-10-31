"""One role's run to its Result: an invalid outcome gets one fix; the deliverable goes to `out`,
records to run.jsonl. A Mode's policy (POLICIES) answers each turn end: None, END or GiveUp.
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
from ensemble.delegate.event.outcome import drop_files
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
PROCESS_EXIT = "process_exit"  # session TurnFailed.error
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
    stops_client = True  # a failed or limited turn's client would hold its container on

    def __init__(self, note: Callable[..., None], driver: Driver | None):
        self.note, self.driver = note, driver

    def fix(self, handle: Session, p: Plan, text: str) -> Session:
        if self.driver is None:
            raise ConfigError("a headless fix needs the session driver")
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
    """A turn end without an outcome gets NUDGE, STOP_LIMIT more give up (a progress mark
    resets it); an IdleTimeout gives up. The session is left open.
    """

    stops_client = False

    def __init__(self, note: Callable[..., None], driver: Driver | None):
        super().__init__(note, driver)
        self.nudged, self.count = False, 0

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
        return GiveUp(idle_reason(seconds))


def claim_release(p: Plan, name: str) -> None:
    """Notes a locked run's release, after a confirmed close."""
    environment = p.session.environment
    lock = None if environment is None else environment.lock
    if not lock:
        return
    say(f"delegate: session {name} closed (lock {lock})")
    RunLog(p.session.workdir).note("release", lock=lock, best_effort=True)


def idle_reason(seconds: float) -> str:
    size, unit = (3600, "h") if seconds >= 3600 else (60, "min") if seconds >= 60 else (1, "s")
    return f"no event for {seconds / size:g} {unit}"


POLICIES = {Mode.HEADLESS: Headless, Mode.TUI: Tui, Mode.IN_PROCESS: Headless}


def say(text: str) -> None:
    to_stderr(printable(text) + "\n")


def to_stderr(text: str) -> None:
    """Dropped once stderr is dead (EIO, EPIPE); its fd then /dev/null, else exit 120 at flush."""
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
    """Run an accepted plan; each Result has one final Finished event."""
    emit = on_event or (lambda _event: None)
    if not driver.session_based:
        instruction = driver.start(p.driver_spec)
        if isinstance(instruction, Instruction):
            result = Result(0, None, instruction=instruction.path)
            emit(Finished(result))
            return result
        raise ConfigError("instruction driver returned a Session")
    if p.spec.mode == Mode.TUI:
        raise ConfigError("tui: its session harness runs it")
    attempts = Attempts(p, emit, driver)
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
    attempts.close(closing(result), result.returncode)
    return result


def closing(result: Result) -> dict:
    """The `result` note's fields."""
    if result.outcome is None:
        return {"error": result.error}
    return {
        "outcome": {k: v for k, v in asdict(result.outcome).items() if k != "deliverable"},
        **({"error": result.error} if result.error else {}),
    }


def stopped(error: BaseException) -> Result:
    """A raised run's code (a Stopped's or int SystemExit's, else 1) and `stopped: <Type>`."""
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


class Attempt:
    """One task attempt fed its session's events: a Result ends it; a headless fix replaces handle."""

    def __init__(
        self,
        p: Plan,
        policy: Headless,
        handle: Session,
        *,
        note: Callable[..., None],
        on_event: Callable[[Event], None],
        first: bool,
    ):
        self.plan, self.policy, self.handle = p, policy, handle
        self.note, self.on_event, self.first = note, on_event, first
        self.seen: set[str] = set()
        self.fixed = False
        self._pass()

    def _pass(self) -> None:
        self.rc: int | None = None
        self.last: str | None = None
        self.replied: str | None = None
        self.limit: Limit | None = None
        self.failed: TurnFailed | None = None

    def feed(self, event: Event | object) -> Result | None:
        if self.failed is not None and not isinstance(event, Exited | Stderr):
            return None
        answer = None
        if isinstance(event, Reply):
            self.last = self.replied = self.reply(event.text)
        elif isinstance(event, TurnEnd):
            if event.reply and event.reply != self.replied:
                # A headless turn repeats its Reply at turn end.
                self.last = self.reply(event.reply)
            self.replied = None
            if not self.limit:
                answer = self.policy.turn_end(self.handle, self.last or "", event.pending)
        elif isinstance(event, Stderr):
            say(event.text)
        elif isinstance(event, IdleTimeout):
            return self.idle(event.seconds)
        elif isinstance(event, TurnFailed):
            if not self.limit:
                self.note("turn_failed", error=event.error, detail=event.detail)
            self.failed = event
            if self.plan.spec.mode != Mode.HEADLESS:
                return self.end()
        elif isinstance(event, LimitHit):
            self.note("limit", resets_at=event.resets_at)
            self.limit = Limit(event.resets_at)
            if not self.policy.stops_client or not self.stop():
                return self.end()
        elif isinstance(event, Exited):
            self.rc = event.returncode
            return self.end()
        return self.answered(answer)

    def idle(self, seconds: float) -> Result | None:
        self.note("idle", seconds=seconds)
        return None if self.limit else self.answered(self.policy.idle(seconds))

    def answered(self, answer: str | GiveUp | None) -> Result | None:
        if isinstance(answer, GiveUp):
            self.note("give_up", reason=answer.reason)
            name = self.handle.name
            if name is None:
                raise SessionError("tui session has no name")
            command = f": {self.handle.attach_command()}" if self.handle.showable else ""
            say(f"delegate: {answer.reason}; session {name} left open{command}")
            self.rc, self.last, self.limit, self.failed = 0, None, None, None
            return self.end()
        return self.end() if answer == END else None

    def stop(self) -> bool:
        try:
            self.handle.stop()
        except SessionError as e:
            say(f"delegate: {self.plan.spec.mode}: {e}")
            return False
        return True

    def end(self) -> Result | None:
        """None after a fix."""
        p, rc = self.plan, self.rc or 0
        diagnostic = f"the client exited {rc}" if rc else ""
        if self.limit:
            return Result(rc, None, f"usage limit; {_resets(self.limit.resets_at)}", self.limit)
        if self.failed:
            if self.policy.stops_client:
                self.stop()
            if rc and self.failed.error == PROCESS_EXIT:
                return Result(rc, None, diagnostic)
            detail = f" ({self.failed.detail})" if self.failed.detail else ""
            return Result(rc, None, f"the client's turn failed: {self.failed.error}{detail}")
        if (block := outcome_block(self.last or "")) is None:
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
            if rc or self.fixed:
                return Result(rc, None, diagnostic or f"invalid outcome: {e}")
            self.note("invalid", error=str(e))
            say(f"delegate: invalid outcome: {e}; resuming once")
            try:
                self.handle = self.policy.fix(self.handle, p, FIX.format(error=e))
            except (SessionConfigError, SessionError) as failed:
                return Result(1, None, f"invalid outcome: {e}; resume failed: {failed}")
            self.fixed = True
            self._pass()
            return None
        return self.finish(outcome, rc, updraft)

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
                    if kind == "local":
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
        if self.first and outcome.status != "needs_input" and "start" not in self.seen:
            say("delegate: missing progress mark: start")
            self.note("missing", name="start")
        return Result(rc, outcome, f"the client exited {rc}" if rc else "")


class Attempts:
    def __init__(self, p: Plan, on_event: Callable[[Event], None], driver: Driver):
        self.plan, self.on_event, self.driver = p, on_event, driver
        self.policy = POLICIES[Mode(p.spec.mode)](self.note, driver)
        self.run_dir = p.session.workdir
        self.handle: Session | None = None
        self.refused = False  # the Driver refused the start: nothing to close
        environment = p.session.environment
        self.lock = None if environment is None else environment.lock
        self.released = False
        self.begun = self.stale = False

    def note(self, kind: str, **fields) -> None:
        RunLog(self.run_dir).note(kind, **fields)

    def close(self, result: dict, rc: int) -> None:
        """Best effort: an OSError is printed."""
        for kind, fields in (("result", result), ("end", {"sid": self.plan.spec.sid, "rc": rc})):
            try:
                self.note(kind, **fields)
            except OSError as e:
                say(f"delegate: {e}")

    def start(self) -> Session:
        """A showable tui session started at a display is shown as the run's `show` says, unless
        hidden.
        """
        p = self.plan
        tui = p.spec.mode == Mode.TUI
        session = replace(p.session, name=p.spec.name, events=p.spec.events) if tui else p.session
        handle = self.driver.start(
            replace(p.driver_spec, session=session), idle_timeout=p.run.idle_timeout
        )
        if isinstance(handle, Instruction):
            raise ConfigError("session driver returned an Instruction")
        self.handle = handle
        if not tui:
            return handle
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

    def begin(self) -> Session | Result:
        """The started session, Begun sent; a Result when it did not start."""
        p = self.plan
        aside, stale = self.set_aside_stale_out()
        try:
            handle = self.start()
        except SessionConfigError as e:
            self.refused = True
            self.settle_stale_out(aside, keep=True)
            raise ConfigError(str(e)) from None
        except SessionError as e:
            self.settle_stale_out(aside, keep=False)
            self.note("input", text=p.params.input)
            if isinstance(e, SessionBusy):
                return Result(EXIT_BUSY, None, str(e))
            return Result(1, None, f"{p.spec.mode}: {e}")
        except BaseException:  # a signal or a driver fault: as it was
            self.settle_stale_out(aside, keep=True)
            raise
        self.settle_stale_out(aside, keep=False)
        self.note("input", text=p.params.input)
        if stale:
            self.stale = True
            return Result(1, None, stale)
        self.begun = True
        self.on_event(Begun(p.sid))
        return handle

    def result(self) -> Result:
        p, handle = self.plan, self.begin()
        if isinstance(handle, Result):
            if self.stale and self.handle is not None:
                self.stop(self.handle)
            return handle
        attempt = Attempt(
            p, self.policy, handle, note=self.note, on_event=self.on_event, first=not p.spec.resume
        )
        while True:
            fixed = attempt.fixed
            for event in attempt.handle.events():
                result = attempt.feed(event)
                self.handle = attempt.handle
                if result is not None:
                    return result
                if attempt.fixed != fixed:
                    break
            else:
                if (result := attempt.end()) is not None:
                    self.handle = attempt.handle
                    return result

    def release(self) -> None:
        """Closes a locked headless run's ended session once, then claims its lock released;
        prints a SessionError."""
        if self.lock is None or self.handle is None or self.released:
            return
        self.released = True
        try:
            self.handle.close()
        except SessionError as e:
            say(f"delegate: {self.plan.spec.mode}: close: {e}")
            return
        claim_release(self.plan, self.handle.name or "agent")

    def stop(self, handle: Session) -> bool:
        """A SessionError is printed, not raised; False then, the session maybe running."""
        try:
            handle.stop()
        except SessionError as e:
            say(f"delegate: {self.plan.spec.mode}: {e}")
            return False
        return True

    def set_aside_stale_out(self) -> tuple[str | None, str]:
        """Before the start (its client may write `out` before start returns): a new run's earlier
        `out` moved aside, its path; else None and why it could not, else "".
        """
        s = self.plan.spec
        if not s.out or s.resume or os.path.isdir(s.out):  # a resume keeps its session's draft
            return None, ""
        aside = os.path.join(os.path.dirname(s.out), f".{os.path.basename(s.out)}.stale")
        try:
            os.replace(s.out, aside)
        except FileNotFoundError:
            return None, ""
        except OSError as e:
            return None, f"cannot remove the stale out: {e}"
        return aside, ""

    def settle_stale_out(self, aside: str | None, *, keep: bool) -> None:
        """keep: back as `out` (the Driver refused the run); else removed. Best effort."""
        if aside is None:
            return
        try:
            if keep and (out := self.plan.spec.out):
                os.replace(aside, out)
            else:
                Path(aside).unlink(missing_ok=True)
        except OSError as e:
            say(f"delegate: stale out: {e}")
