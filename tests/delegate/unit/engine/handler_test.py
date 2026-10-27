"""The delegation handler over a real run dir and a fake owner view: it binds the opening attempt
or opens a follow-up on its worker's TurnStarted, runs the attempt core on each EventCall, ends an
attempt (Result, notes, outcome line, a locked run's close, then Finished) once, and ignores calls
that are not its worker's.
"""

import io
import json
import os
import uuid
from dataclasses import replace
from unittest import mock

from ensemble.delegate import Spec
from ensemble.delegate.engine import handler
from ensemble.delegate.engine.record import HANDLER, RunRecord
from ensemble.delegate.engine.types import Layout, Result
from ensemble.session import (
    Activity,
    Blocked,
    EnvironmentSpec,
    EventCall,
    Exited,
    HookCallback,
    LimitHit,
    Reply,
    SessionError,
    TurnEnd,
    TurnFailed,
    TurnStarted,
)
from tests.delegate.harness import SID, HarnessCase
from tests.delegate.replies import block
from tests.delegate.unit.runs import DONE, GAVE_UP, START

NEEDS = {**DONE, "status": "needs_input", "questions": ["Which?"], "deliverable": ""}
NAME = f"dummy-tester-{SID[:8]}"
IDLE = 600.0


class View:
    """The owner's control view as the handler uses it; calls recorded."""

    showable = False
    name = NAME

    def __init__(self):
        self.calls: list[tuple] = []
        self.close_fail: Exception | None = None

    def attach_command(self):
        return f"tmux attach -t '={NAME}'"

    def send(self, text):
        self.calls.append(("send", text))

    def stop(self):
        self.calls.append(("stop",))

    def close(self):
        self.calls.append(("close",))
        if self.close_fail:
            raise self.close_fail


class Timer:
    """threading.Timer, fired by hand."""

    made: list["Timer"] = []

    def __init__(self, interval, function):
        self.interval, self.function = interval, function
        self.started = self.cancelled = False
        self.daemon = False
        Timer.made.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.function()


class Child:
    """subprocess.Popen for the callback: keeps each line, with the record's state at delivery."""

    def __init__(self, case, argv):
        self.case, self.pid, self.returncode = case, 4321, 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def communicate(self, line, timeout=None):
        self.case.sent.append((json.loads(line), self.case.metadata()))
        return "", ""


class HandlerCase(HarnessCase):
    def setUp(self):
        super().setUp()
        self.events = os.path.join(self.home, "events.log")
        spec = Spec("input", self.work, task="echo", sid=SID, client="fake", mode="tui")
        self.delegate = self.make(spec=replace(spec, events=self.events), callback=("callback",))
        self.record = RunRecord(Layout(self.work))
        self.register(1)
        self.view, self.sent, self.seq = View(), [], 0
        self.generation = uuid.uuid4().hex
        Timer.made = []
        self.enterContext(mock.patch("threading.Timer", Timer))
        self.enterContext(
            mock.patch("subprocess.Popen", side_effect=lambda argv, **_kw: Child(self, argv))
        )
        self.err = self.enterContext(mock.patch("sys.stderr", new_callable=io.StringIO))

    def register(self, token, **changes) -> None:
        state = self.record.read()
        handlers = (HookCallback(HANDLER, {"workdir": self.work, "worker": token}, ()),)
        run = replace(state.plan.run, idle_timeout=IDLE)
        session = replace(state.plan.session, handlers=handlers)
        self.record.save(
            replace(state, plan=replace(state.plan, session=session, run=run), **changes)
        )

    def call(self, event, *, token=1, sid=SID, generation=None, event_id=None, view=None):
        self.seq += 1
        generation = generation or self.generation
        handler.on_event(
            EventCall(
                session=view or self.view,
                event=event,
                sid=sid,
                generation=generation,
                event_id=event_id or f"{generation}:{self.seq}",
                turn=None,
                state={"workdir": self.work, "worker": token},
            )
        )

    def state(self):
        return self.record.read()

    def kinds(self) -> list[str]:
        path = os.path.join(self.state().plan.session.workdir, "run.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line)["kind"] for line in f]

    def lines(self) -> list[str]:
        if not os.path.exists(self.events):
            return []
        with open(self.events, encoding="utf-8") as f:
            return [line.split()[-2:] for line in f if line.strip()]

    def kinds_sent(self) -> list[tuple[str, int]]:
        return [(line["kind"], line["attempt"]) for line, _ in self.sent]

    def finish(self, data=DONE):
        self.call(TurnStarted())
        self.call(TurnEnd(f"{START}\n{block(data)}", 0))


class BindTest(HandlerCase):
    def test_the_first_call_binds_the_opening_attempt_its_drive_began(self):
        self.assertFalse(self.state().bound)
        self.call(TurnStarted())
        state = self.state()
        self.assertEqual((state.attempt, state.bound, state.completed), (1, True, False))
        self.assertEqual(self.kinds_sent(), [])

    def test_an_outcome_ends_the_attempt_stored_before_its_finished(self):
        self.finish()
        state = self.state()
        self.assertTrue(state.completed)
        self.assertEqual(state.result.outcome.status, "done")
        self.assertEqual(self.kinds_sent(), [("progress", 1), ("finished", 1)])
        finished, at = self.sent[-1]
        self.assertEqual(finished["outcome"]["status"], "done")
        self.assertTrue(at["completed"])
        self.assertEqual(self.kinds()[-2:], ["result", "end"])
        self.assertEqual(self.lines(), [["outcome", "done"]])
        self.assertEqual(self.view.calls, [])

    def test_a_startup_exited_or_limit_binds_and_ends_the_attempt(self):
        self.call(Exited(3))
        state = self.state()
        self.assertEqual(state.result, Result(3, None, "the client exited 3"))
        self.assertEqual(self.kinds_sent(), [("finished", 1)])
        self.assertEqual(self.lines(), [["outcome", "error"]])

    def test_a_worker_killed_by_a_signal_is_an_interrupted_attempt(self):
        self.call(TurnStarted())
        self.call(Exited(-1))
        self.assertEqual(self.state().result, Result(129, None, "stopped: Stopped: 129"))

    def test_a_limit_stops_the_worker_and_its_exit_ends_the_attempt(self):
        self.call(TurnStarted())
        self.call(LimitHit(None))
        self.assertEqual(self.view.calls, [("stop",)])
        self.assertFalse(self.state().completed)
        self.call(Exited(0))
        result = self.state().result
        self.assertEqual(result.error, "usage limit; reset time unknown")
        self.assertIsNotNone(result.limit)


class FollowUpTest(HandlerCase):
    def test_an_answer_after_needs_input_opens_and_ends_attempt_two(self):
        self.finish(NEEDS)
        first = self.state().result
        self.assertEqual(first.outcome.status, "needs_input")
        self.call(Reply("ignored after completion"))
        self.assertEqual(self.state().attempt, 1)
        self.call(TurnStarted())
        state = self.state()
        self.assertEqual(
            (state.attempt, state.completed, state.bound, state.previous), (2, False, True, first)
        )
        self.call(TurnEnd(block(DONE), 0))
        state = self.state()
        self.assertEqual((state.attempt, state.result.outcome.status), (2, "done"))
        self.assertEqual(
            self.kinds_sent(),
            [
                ("progress", 1),
                ("finished", 1),
                ("begun", 2),
                ("finished", 2),
            ],
        )
        self.assertEqual(self.lines(), [["outcome", "needs_input"], ["outcome", "done"]])
        self.assertNotIn("missing", self.kinds())

    def test_continue_after_a_failed_turn_opens_the_next_attempt(self):
        self.call(TurnStarted())
        self.call(TurnFailed("server_overloaded", "busy"))
        self.assertEqual(
            self.state().result.error, "the client's turn failed: server_overloaded (busy)"
        )
        self.call(TurnStarted())
        self.call(TurnEnd(block(DONE), 0))
        state = self.state()
        self.assertEqual((state.attempt, state.result.outcome.status), (2, "done"))

    def test_an_exit_after_completion_ends_nothing(self):
        self.finish()
        self.call(Exited(0))
        self.assertEqual(self.state().result.outcome.status, "done")
        self.assertEqual(self.kinds_sent()[-1], ("finished", 1))


class CloseTest(HandlerCase):
    """FR-34: a close ends the active attempt once, as a stop, whatever code its client exits with."""

    def test_a_close_ends_the_active_attempt_as_a_stop_before_the_worker_exits(self):
        for opening in (True, False):
            with self.subTest(opening=opening):
                if not opening:
                    self.call(TurnStarted())
                self.delegate.close()
                state = self.state()
                self.assertEqual(state.result, Result(129, None, "stopped: Stopped: 129"))
                self.assertEqual(self.kinds_sent()[-1], ("finished", state.attempt))
                self.assertEqual(self.kinds()[-2:], ["result", "end"])
                sent = len(self.sent)
                self.call(Exited(0))
                self.assertEqual(self.state().result, state.result)
                self.assertEqual(len(self.sent), sent)
                self.register(state.attempt + 1, attempt=state.attempt + 1, completed=False)
                self.record.save(replace(self.state(), result=None, bound=False))

    def test_a_close_after_the_attempt_ended_sends_no_second_finished(self):
        self.finish()
        before = (self.state(), list(self.sent))
        self.delegate.close()
        self.assertEqual((self.state(), self.sent), before)


class IgnoredTest(HandlerCase):
    def test_a_redelivered_event_changes_nothing(self):
        self.call(TurnStarted(), event_id="g:1")
        self.call(TurnEnd(block(DONE), 0), event_id="g:2")
        before = (self.state(), list(self.sent), self.kinds())
        self.call(TurnEnd(block(DONE), 0), event_id="g:2")
        self.assertEqual((self.state(), self.sent, self.kinds()), before)

    def test_calls_that_are_not_the_runs_worker_are_ignored(self):
        before = self.metadata()
        self.call(TurnStarted(), sid="other")
        self.call(TurnStarted(), token=2)
        self.call(TurnStarted(), token="1")
        self.assertEqual(self.metadata(), before)
        state = self.state()
        self.record.save(
            replace(state, plan=replace(state.plan, spec=replace(state.plan.spec, mode="headless")))
        )
        self.call(TurnStarted())
        self.assertFalse(self.state().bound)
        self.register(1)
        state = self.state()
        self.record.save(
            replace(
                state, plan=replace(state.plan, session=replace(state.plan.session, handlers=()))
            )
        )
        self.call(TurnStarted())
        self.assertFalse(self.state().bound)
        self.assertEqual(self.sent, [])

    def test_a_closed_workers_calls_leave_the_resumed_attempt_alone(self):
        for spoke in (False, True):
            with self.subTest(spoke=spoke):
                old, closed = uuid.uuid4().hex, self.state().attempt
                if spoke:
                    self.call(TurnStarted(), generation=old, token=closed)
                self.register(closed + 1, attempt=closed + 1, bound=False)
                self.call(Exited(0), generation=old, token=closed)
                state = self.state()
                self.assertEqual((state.completed, state.bound), (False, False))
                self.call(TurnStarted(), token=state.attempt)
                self.assertTrue(self.state().bound)

    def test_an_unreadable_record_is_printed_and_the_call_handled(self):
        with open(self.state_record(), "w", encoding="utf-8") as f:
            f.write("{")
        self.assertIsNone(self.call(TurnStarted()))
        self.assertIn("delegate: invalid", self.err.getvalue())


class LockedTest(HandlerCase):
    def setUp(self):
        super().setUp()
        state = self.state()
        environment = EnvironmentSpec(image="dummy-env:test", gateway="workers", lock="dummy-lock")
        session = replace(state.plan.session, environment=environment)
        self.record.save(replace(state, plan=replace(state.plan, session=session)))

    def test_each_ending_closes_through_the_owner_then_claims_release(self):
        self.finish()
        self.assertEqual(self.view.calls, [("close",)])
        self.assertIn(f"delegate: session {NAME} closed (lock dummy-lock)", self.err.getvalue())
        self.assertIn("release", self.kinds())
        self.assertEqual(self.kinds_sent()[-1], ("finished", 1))

    def test_a_close_ends_the_attempt_and_claims_no_release_without_a_confirmed_close(self):
        self.call(TurnStarted())
        self.delegate.close()
        self.assertEqual(self.state().result, Result(129, None, "stopped: Stopped: 129"))
        self.assertEqual(self.kinds_sent()[-1], ("finished", 1))
        self.assertNotIn("release", self.kinds())

    def test_a_close_that_fails_claims_no_release(self):
        self.view.close_fail = SessionError("not confirmed")
        self.finish()
        self.assertIn("delegate: tui: close: not confirmed", self.err.getvalue())
        self.assertNotIn("closed (lock", self.err.getvalue())
        self.assertNotIn("release", self.kinds())
        self.assertEqual(self.kinds_sent()[-1], ("finished", 1))

    def test_an_idle_give_up_leaves_finished_to_its_closes_exit(self):
        self.call(TurnStarted())
        Timer.made[-1].fire()
        self.assertEqual(self.state().result, GAVE_UP)
        self.assertEqual(self.view.calls, [("close",)])
        self.assertEqual(self.kinds_sent(), [])
        self.call(Exited(0))
        self.assertEqual(self.kinds_sent(), [("finished", 1)])

    def test_an_idle_give_up_whose_close_fails_finishes_at_once(self):
        self.call(TurnStarted())
        self.view.close_fail = SessionError("not confirmed")
        Timer.made[-1].fire()
        self.assertEqual(self.kinds_sent(), [("finished", 1)])
        self.call(Exited(0))
        self.assertEqual(self.kinds_sent(), [("finished", 1)])


class EndingTest(HandlerCase):
    def test_an_attempt_another_owner_completed_first_is_left_alone(self):
        self.call(TurnStarted())
        stored = Result(1, None, "stopped: session harness ended without a Result")
        self.assertTrue(self.record.complete(1, stored, sid=SID))
        before = self.kinds()
        self.call(TurnEnd(block(DONE), 0))
        self.assertEqual(self.state().result, stored)
        self.assertEqual(self.kinds(), before)
        self.assertEqual(self.kinds_sent(), [])
        self.assertEqual(self.lines(), [])


class IdleTest(HandlerCase):
    def live(self) -> list[Timer]:
        return [t for t in Timer.made if t.started and not t.cancelled]

    def test_the_timer_runs_while_bound_and_not_blocked(self):
        self.call(TurnStarted())
        (first,) = self.live()
        self.assertEqual(first.interval, IDLE)
        self.call(Activity("Read"))
        self.assertTrue(first.cancelled)
        self.assertEqual(len(self.live()), 1)
        self.call(Blocked("Edit"))
        self.assertEqual(self.live(), [])
        self.call(Reply("still asking"))
        self.assertEqual(self.live(), [])
        self.call(Activity("Edit"))
        self.assertEqual(len(self.live()), 1)
        self.call(TurnEnd(block(DONE), 0))
        self.assertEqual(self.live(), [])

    def test_an_expiry_gives_the_attempt_up_leaving_the_session_open(self):
        self.call(TurnStarted())
        self.live()[0].fire()
        self.assertEqual(self.state().result, GAVE_UP)
        self.assertIn("give_up", self.kinds())
        self.assertIn(f"session {NAME} left open", self.err.getvalue())
        self.assertEqual(self.view.calls, [])
        self.assertEqual(self.kinds_sent()[-1], ("finished", 1))
        self.assertEqual(self.lines(), [["outcome", "error"]])

    def test_a_stale_timer_ends_nothing(self):
        self.call(TurnStarted())
        stale = self.live()[0]
        self.call(TurnEnd(block(DONE), 0))
        stale.fire()
        self.assertEqual(self.state().result.outcome.status, "done")


class FailureTest(HandlerCase):
    def test_a_deliverable_it_cannot_save_ends_the_attempt_stopped(self):
        blocker = os.path.join(self.home, "a-file")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("")
        state = self.state()
        spec = replace(state.plan.spec, out=os.path.join(blocker, "out.md"))
        self.record.save(replace(state, plan=replace(state.plan, spec=spec)))
        self.finish()
        result = self.state().result
        self.assertTrue(self.state().completed)
        self.assertTrue(result.error.startswith("stopped: FileExistsError: "), result.error)
        self.assertEqual(self.kinds_sent()[-1], ("finished", 1))
        self.assertEqual(self.lines(), [["outcome", "error"]])
