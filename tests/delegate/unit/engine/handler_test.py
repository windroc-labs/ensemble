"""The delegation handler over a real run dir and a fake owner view: it binds the opening attempt
or opens a follow-up on its worker's TurnStarted, runs the attempt core on each EventCall, ends an
attempt (Result, notes, outcome line, then Finished) once, closing no worker, and ignores calls
that are not its worker's.
"""

import contextlib
import io
import json
import os
import uuid
from collections.abc import Callable
from dataclasses import replace
from unittest import mock

from ensemble.delegate import Spec
from ensemble.delegate.engine import handler
from ensemble.delegate.engine.record import HANDLER, RunRecord
from ensemble.delegate.engine.runner import PROCESS_EXIT, STOP_LIMIT, stopped
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
    Unanswered,
)
from ensemble.utils import Status, StatusState
from tests.delegate.fakes.callback_child import CallbackChild
from tests.delegate.fakes.harness_session import HarnessSession, RecordingHost
from tests.delegate.harness import SID, HarnessCase, ViewedHarnessDelegate
from tests.delegate.replies import block
from tests.delegate.unit.runs import DONE, GAVE_UP, START
from tests.utils.jsonl import read_jsonl

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

    def alive(self):
        return True

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


class OwnerRefused(SessionError):
    """As ensemble.session's internal one, which delegate tests may not import."""


class Agent:
    """The worker's session as Delegate.close attaches it; close runs `on_close`."""

    mode = "tui"

    def __init__(self, on_close, *, live=True):
        self.on_close, self.live = on_close, live

    def alive(self):
        return self.live

    def status(self):
        state = StatusState.WORKING if self.live else StatusState.GONE
        return Status(
            name="agent", alive=self.live, state=state, outcome=None, attach=None, hidden=False
        )

    def close(self):
        self.on_close()


class AgentSession(HarnessSession):
    """attach: `attaching` called, then `refusal` raised, else `agent`, else HarnessSession's (no
    session)."""

    agent: Agent | None = None
    refusal: Exception | None = None
    attaching = None

    @classmethod
    def attach(cls, name, **kw):
        if cls.attaching is not None:
            cls.attaching()
        if cls.refusal is not None:
            raise cls.refusal
        return cls.agent if cls.agent is not None else super().attach(name, **kw)


class HandlerCase(HarnessCase):
    def setUp(self):
        super().setUp()
        self.events = os.path.join(self.home, "events.log")
        spec = Spec("input", self.work, task="echo", sid=SID, client="fake", mode="tui")
        AgentSession.agent = AgentSession.refusal = AgentSession.attaching = None
        self.delegate = self.make(
            spec=replace(spec, events=self.events), callback=("callback",), session_cls=AgentSession
        )
        self.record = RunRecord(Layout(self.work))
        self.register(1)
        self.view, self.sent, self.seq = View(), [], 0
        self.racing: Callable[[], None] | None = None
        self.generation = uuid.uuid4().hex
        Timer.made = []
        self.enterContext(mock.patch("threading.Timer", Timer))
        self.enterContext(
            mock.patch(
                "subprocess.Popen", side_effect=lambda _argv, **_kw: CallbackChild(self.keep)
            )
        )
        self.err = self.enterContext(mock.patch("sys.stderr", new_callable=io.StringIO))

    def keep(self, line: str) -> None:
        """The callback's delivery: keeps each line, with the record's state at delivery."""
        if self.racing is not None:
            self.racing()
        self.sent.append((json.loads(line), self.metadata()))

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
        return [row["kind"] for row in read_jsonl(path, missing_ok=True)]

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

    def test_a_run_without_a_callback_sends_no_event(self):
        self.record.save(replace(self.state(), callback=None))
        self.finish()
        self.assertEqual(self.state().result.outcome.status, "done")
        self.assertEqual(self.sent, [])

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

    def test_a_worker_killed_mid_turn_is_an_interrupted_attempt(self):
        """A kill with a turn open (the nudge's, say) and no close claimed: its turn's exit
        failure waits for the exit, whose signal makes the attempt interrupted, not failed.
        """
        for failed in (
            TurnFailed(PROCESS_EXIT, "worker exited -9"),
            TurnFailed("exited", "worker exited -9 mid-turn"),
        ):
            with self.subTest(failed=failed):
                n = self.state().attempt
                self.call(TurnStarted(), token=n)
                self.call(failed, token=n)
                self.assertFalse(self.state().completed)
                self.assertNotIn(("finished", n), self.kinds_sent())
                self.call(Exited(-9), token=n)
                state = self.state()
                self.assertEqual(state.result, Result(137, None, "stopped: Stopped: 137"))
                self.assertEqual([k for k in self.kinds_sent() if k[1] == n], [("finished", n)])
                self.register(n + 1, attempt=n + 1, completed=False)
                self.record.save(replace(self.state(), result=None, bound=False))
                self.generation = uuid.uuid4().hex

    def test_a_worker_exiting_nonzero_mid_turn_is_a_failed_attempt_with_its_code(self):
        self.call(TurnStarted())
        self.call(TurnFailed(PROCESS_EXIT, "worker exited 1"))
        self.call(Exited(1))
        self.assertEqual(self.state().result, Result(1, None, "the client exited 1"))
        self.assertEqual(self.kinds_sent()[-1], ("finished", 1))

    def test_a_limit_ends_the_attempt_and_leaves_the_worker(self):
        self.call(TurnStarted())
        self.call(LimitHit(None))
        self.assertEqual(self.view.calls, [])
        result = self.state().result
        self.assertEqual(result.error, "usage limit; reset time unknown")
        self.assertIsNotNone(result.limit)
        self.assertEqual(self.kinds_sent(), [("finished", 1)])
        self.call(Exited(0))
        self.assertEqual(self.state().result, result)
        self.assertEqual(self.kinds_sent(), [("finished", 1)])


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

    def test_input_after_a_stop_its_worker_survived_opens_the_next_attempt(self):
        self.call(TurnStarted())
        self.assertTrue(self.record.complete(1, stopped(OSError("disk full")), sid=SID))
        self.call(TurnStarted())
        self.assertEqual((self.state().attempt, self.state().completed), (2, False))

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

    def test_a_fix_nudges_turn_stays_in_its_attempt(self):
        self.call(TurnStarted())
        self.call(TurnEnd("no outcome block yet", 0))
        self.assertEqual(len([c for c in self.view.calls if c[0] == "send"]), 1)
        self.call(TurnStarted())
        self.call(TurnEnd(block(DONE), 0))
        state = self.state()
        self.assertEqual((state.attempt, state.result.outcome.status), (1, "done"))
        self.assertEqual(self.kinds_sent(), [("finished", 1)])

    def test_an_input_resume_of_a_live_owned_worker_opens_one_attempt(self):
        self.finish(NEEDS)
        worker = self.view

        class Live(ViewedHarnessDelegate):
            """Its owned worker still runs: the answer is only sent to it."""

            def _answered_agent(self, state):
                return worker

        Live.attach(self.work, session_cls=HarnessSession).resume(input="The answer.")
        self.assertEqual(self.view.calls[-1], ("send", "The answer."))
        self.assertEqual((self.state().attempt, self.state().completed), (1, True))
        self.call(TurnStarted())
        self.call(TurnStarted())
        self.call(TurnEnd(block(DONE), 0))
        state = self.state()
        self.assertEqual((state.attempt, state.result.outcome.status), (2, "done"))
        self.assertEqual(
            self.kinds_sent(),
            [("progress", 1), ("finished", 1), ("begun", 2), ("finished", 2)],
        )

    def test_an_answer_to_a_worker_a_failed_close_left_opens_its_attempt(self):
        self.finish(NEEDS)
        self.record.save(replace(self.state(), closing=True))
        worker = self.view

        class Live(ViewedHarnessDelegate):
            """A close whose agent close failed left its worker running."""

            def _answered_agent(self, state):
                return worker

        Live.attach(self.work, session_cls=HarnessSession).resume(input="The answer.")
        self.assertFalse(self.state().closing)
        self.call(TurnStarted())
        self.assertEqual((self.state().attempt, self.state().completed), (2, False))

    def test_an_exit_after_completion_ends_nothing(self):
        self.finish()
        self.call(Exited(0))
        self.assertEqual(self.state().result.outcome.status, "done")
        self.assertEqual(self.kinds_sent()[-1], ("finished", 1))


class CloseTest(HandlerCase):
    """FR-34: a close holds a claim that keeps its worker's input from opening a follow-up, and
    ends the active attempt once, as a stop, only by the exit it causes.
    """

    STOP = Result(143, None, "stopped: Stopped: 143")

    def closing(self, during, refusal=None):
        def close():
            during()
            if refusal is not None:
                raise refusal

        AgentSession.agent = Agent(close)

    def test_a_close_ends_the_opening_attempt_as_a_stop_before_the_worker_exits(self):
        self.delegate.close()
        state = self.state()
        self.assertEqual(state.result, self.STOP)
        self.assertEqual(self.kinds_sent()[-1], ("finished", state.attempt))
        self.assertEqual(self.kinds()[-2:], ["result", "end"])
        sent = len(self.sent)
        self.call(Exited(0))
        self.assertEqual(self.state().result, state.result)
        self.assertEqual(len(self.sent), sent)

    def test_its_claim_covers_the_attach_and_the_owners_close(self):
        self.call(TurnStarted())
        seen = []
        AgentSession.attaching = lambda: seen.append(self.record.store.closing())
        self.closing(lambda: seen.append((self.record.store.closing(), self.state().completed)))
        self.delegate.close()
        self.assertEqual(seen, [True, (True, False)])
        self.assertFalse(self.record.store.closing())
        self.assertEqual(self.state().result, self.STOP)
        self.assertEqual(self.kinds_sent(), [("finished", 1)])

    def test_the_exit_it_causes_ends_the_attempt_as_its_stop_once(self):
        for events in (
            (TurnFailed("exited", "worker exited mid-turn"), Exited(0)),
            (Exited(1),),
            (TurnStarted(), TurnFailed("exited", "worker exited -1 mid-turn"), Exited(-1)),
            (TurnStarted(), TurnFailed(PROCESS_EXIT, "worker exited -15"), Exited(-15)),
        ):
            with self.subTest(events=events):
                n = self.state().attempt
                self.call(TurnStarted(), token=n)
                self.closing(lambda events=events, n=n: [self.call(e, token=n) for e in events])
                self.delegate.close()
                state = self.state()
                self.assertEqual((state.attempt, state.result), (n, self.STOP))
                self.assertEqual(self.kinds_sent()[-1], ("finished", n))
                self.assertEqual([k for k in self.kinds_sent() if k[1] == n], [("finished", n)])
                self.register(n + 1, attempt=n + 1, completed=False)
                self.record.save(replace(self.state(), result=None, bound=False))
                self.generation = uuid.uuid4().hex

    def test_input_on_an_ended_attempt_while_closing_opens_no_follow_up(self):
        self.finish(NEEDS)
        sent = list(self.sent)
        self.closing(lambda: [self.call(TurnStarted()), self.call(Exited(0))])
        self.delegate.close()
        state = self.state()
        self.assertEqual((state.attempt, state.result.outcome.status), (1, "needs_input"))
        self.assertEqual(self.sent, sent)

    def test_a_follow_up_opened_before_the_claim_ends_as_its_stop(self):
        self.finish(NEEDS)
        self.call(TurnStarted())
        self.assertEqual(self.state().attempt, 2)
        self.closing(
            lambda: [
                self.call(TurnFailed("exited", "worker exited mid-turn")),
                self.call(Exited(0)),
            ]
        )
        self.delegate.close()
        self.assertEqual((self.state().attempt, self.state().result), (2, self.STOP))
        self.assertEqual(self.kinds_sent()[-2:], [("begun", 2), ("finished", 2)])

    def test_a_follow_up_its_handler_left_active_ends_as_its_stop_under_its_own_number(self):
        self.finish(NEEDS)
        claim = self.record.store.closing_claim

        @contextlib.contextmanager
        def late():  # input lands before the claim; then its handler never ends it
            self.call(TurnStarted())
            with claim():
                yield

        with mock.patch.object(type(self.delegate.store), "closing_claim", lambda _store: late()):
            self.delegate.close()
        self.assertEqual((self.state().attempt, self.state().result), (2, self.STOP))
        self.assertEqual(self.kinds_sent()[-2:], [("begun", 2), ("finished", 2)])

    def test_endings_it_does_not_cause_keep_their_own_result(self):
        self.call(TurnStarted())
        self.closing(lambda: [self.call(TurnEnd(block(DONE), 0)), self.call(Exited(0))])
        self.delegate.close()
        self.assertEqual(self.state().result.outcome.status, "done")
        self.assertEqual(self.kinds_sent(), [("finished", 1)])

    def test_a_failed_turn_while_closing_keeps_its_failure(self):
        self.call(TurnStarted())
        self.closing(lambda: self.call(TurnFailed("server_overloaded", "busy")))
        self.delegate.close()
        self.assertEqual(
            self.state().result.error, "the client's turn failed: server_overloaded (busy)"
        )
        self.assertEqual(self.kinds_sent(), [("finished", 1)])

    def test_a_refused_close_drops_its_claim_and_loses_no_event(self):
        for refusal in (
            SessionError("cannot close"),
            OwnerRefused("cannot close w1"),  # the owner's refusal, a SessionError
            Unanswered("not confirmed"),
        ):
            with self.subTest(refusal=type(refusal).__name__):
                n = self.state().attempt
                self.call(TurnStarted(), token=n)
                self.closing(lambda n=n: self.call(TurnEnd(block(NEEDS), 0), token=n), refusal)
                with self.assertRaises(type(refusal)):
                    self.delegate.close()
                self.assertFalse(self.record.store.closing())
                self.assertEqual(self.state().result.outcome.status, "needs_input")
                self.call(TurnStarted(), token=n)
                self.assertEqual((self.state().attempt, self.state().completed), (n + 1, False))
                self.register(n + 1)

    def test_an_idle_give_up_while_closing_keeps_its_own_result(self):
        self.call(TurnStarted())
        self.closing(lambda: Timer.made[-1].fire())
        self.delegate.close()
        self.assertEqual(self.state().result, GAVE_UP)
        self.assertEqual(self.kinds_sent(), [("finished", 1)])

    def test_a_refused_close_leaves_the_attempt_active_and_reports_nothing(self):
        self.call(TurnStarted())
        self.closing(lambda: None, SessionError("cannot close"))
        with self.assertRaisesRegex(SessionError, "cannot close"):
            self.delegate.close()
        state = self.state()
        self.assertEqual((state.completed, state.result), (False, None))
        self.assertEqual(self.kinds_sent(), [])
        self.assertNotIn("result", self.kinds())
        self.call(TurnEnd(block(DONE), 0))  # its worker goes on, and its handler with it
        self.assertEqual(self.state().result.outcome.status, "done")

    def test_input_taken_on_an_ended_attempt_during_a_refused_close_is_not_a_follow_up(self):
        self.finish(NEEDS)
        self.closing(lambda: self.call(TurnStarted()), SessionError("cannot close"))
        with self.assertRaises(SessionError):
            self.delegate.close()
        self.assertEqual(self.state().attempt, 1)
        self.call(TurnStarted())
        self.assertEqual((self.state().attempt, self.state().completed), (2, False))

    def test_an_unanswered_agent_leaves_the_attempt_alone(self):
        self.call(TurnStarted())
        AgentSession.refusal = Unanswered("host down")
        with self.assertRaises(Unanswered):
            self.delegate.close()
        self.assertFalse(self.state().completed)
        self.assertFalse(self.record.store.closing())
        self.assertEqual(self.kinds_sent(), [])


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
    """FR-23: a locked run is its user's to close, as an unlocked one is: no ending closes or stops
    it, and only a confirmed close claims its lock released.
    """

    def setUp(self):
        super().setUp()
        state = self.state()
        environment = EnvironmentSpec(image="dummy-env:test", gateway="workers", lock="dummy-lock")
        session = replace(state.plan.session, environment=environment)
        self.record.save(replace(state, plan=replace(state.plan, session=session)))

    def assert_left_running(self) -> Result:
        state = self.state()
        self.assertTrue(state.completed)
        self.assertFalse({("close",), ("stop",)} & set(self.view.calls), self.view.calls)
        self.assertNotIn("release", self.kinds())
        self.assertNotIn("closed (lock", self.err.getvalue())
        self.assertEqual([k for k in self.kinds_sent() if k[0] == "finished"], [("finished", 1)])
        self.assertEqual(self.kinds()[-2:], ["result", "end"])
        return state.result

    def test_a_done_outcome_leaves_it_running(self):
        self.finish()
        self.assertEqual(self.assert_left_running().outcome.status, "done")

    def test_needs_input_leaves_it_running(self):
        self.finish(NEEDS)
        self.assertEqual(self.assert_left_running().outcome.status, "needs_input")

    def test_a_failed_turn_leaves_it_running(self):
        self.call(TurnStarted())
        self.call(TurnFailed("server_overloaded", "529"))
        self.assertEqual(
            self.assert_left_running().error, "the client's turn failed: server_overloaded (529)"
        )

    def test_a_nudge_give_up_leaves_it_running(self):
        self.call(TurnStarted())
        for _ in range(1 + STOP_LIMIT):
            self.call(TurnEnd("Working.", 0))
        self.assertEqual(self.assert_left_running(), GAVE_UP)
        self.assertIn(f"session {NAME} left open", self.err.getvalue())

    def test_an_idle_give_up_leaves_it_running(self):
        self.call(TurnStarted())
        next(t for t in Timer.made if t.started and not t.cancelled).fire()
        self.assertEqual(self.assert_left_running(), GAVE_UP)
        self.assertIn(f"session {NAME} left open", self.err.getvalue())

    def test_a_usage_limit_leaves_it_running(self):
        self.call(TurnStarted())
        self.call(LimitHit(None))
        self.assertIsNotNone(self.assert_left_running().limit)

    def test_an_answer_opens_the_next_attempt_in_the_same_worker(self):
        self.finish(NEEDS)
        self.call(TurnStarted())
        state = self.state()
        self.assertEqual((state.attempt, state.completed), (2, False))
        self.call(TurnEnd(block(DONE), 0))
        self.assertEqual(self.state().result.outcome.status, "done")
        self.assertEqual(self.view.calls, [])
        self.assertNotIn("release", self.kinds())

    def test_the_clients_own_exit_ends_the_attempt_and_claims_nothing(self):
        for rc, result in (
            (1, Result(1, None, "the client exited 1")),
            (-9, Result(137, None, "stopped: Stopped: 137")),
        ):
            with self.subTest(rc=rc):
                n = self.state().attempt
                self.call(TurnStarted(), token=n)
                self.call(Exited(rc), token=n)
                self.assertEqual(self.state().result, result)
                self.assertEqual(self.view.calls, [])
                self.assertNotIn("release", self.kinds())
                self.assertNotIn("closed (lock", self.err.getvalue())
                self.register(n + 1, attempt=n + 1, completed=False)
                self.record.save(replace(self.state(), result=None, bound=False))
                self.generation = uuid.uuid4().hex

    def test_a_gone_workers_close_ends_the_attempt_and_claims_no_release(self):
        self.call(TurnStarted())
        self.delegate.close()
        self.assertEqual(self.state().result, Result(143, None, "stopped: Stopped: 143"))
        self.assertEqual(self.kinds_sent()[-1], ("finished", 1))
        self.assertNotIn("release", self.kinds())

    def test_a_confirmed_close_claims_release_then_finishes(self):
        self.call(TurnStarted())
        seen = []
        AgentSession.agent = Agent(
            lambda: seen.append(("release" in self.kinds(), list(self.sent)))
        )
        self.delegate.close()
        self.assertEqual(seen, [(False, [])])
        self.assertEqual(self.kinds()[-3:], ["result", "end", "release"])
        self.assertEqual(self.kinds_sent(), [("finished", 1)])

    def test_a_close_after_a_locked_ending_claims_release_without_a_second_finished(self):
        self.finish(NEEDS)
        AgentSession.agent = Agent(lambda: None)
        self.delegate.close()
        self.assertEqual(self.kinds()[-1], "release")
        self.assertEqual(self.kinds().count("end"), 1)
        self.assertIn(f"delegate: session {NAME} closed (lock dummy-lock)", self.err.getvalue())
        self.assertEqual([k for k in self.kinds_sent() if k[0] == "finished"], [("finished", 1)])

    def test_closing_a_worker_whose_client_exited_claims_no_release(self):
        self.finish(NEEDS)
        AgentSession.agent = Agent(lambda: None, live=False)
        self.delegate.close()
        self.assertNotIn("release", self.kinds())
        self.assertNotIn("closed (lock", self.err.getvalue())

    def test_an_unconfirmed_close_claims_no_release(self):
        self.finish(NEEDS)
        for refusal in (OwnerRefused("cannot close w1"), Unanswered("not confirmed")):
            with self.subTest(refusal=type(refusal).__name__):

                def refuse(refusal=refusal):
                    raise refusal

                AgentSession.agent = Agent(refuse)
                with self.assertRaises(type(refusal)):
                    self.delegate.close()
                self.assertNotIn("release", self.kinds())
                self.assertNotIn("closed (lock", self.err.getvalue())
        AgentSession.agent = Agent(lambda: None)
        self.delegate.close()
        self.assertEqual(self.kinds()[-1], "release")


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

    LOST = Result(1, None, "stopped: session harness ended without a Result")

    def lost(self, event) -> None:
        """Another owner completes the attempt while the handler decides `event`: no note."""
        self.call(TurnStarted())
        before = self.kinds()
        self.call(event)
        self.assertEqual(self.state().result, self.LOST)
        self.assertEqual(self.kinds(), before)
        self.assertNotIn(("finished", 1), self.kinds_sent())  # a Progress event is not stored

    def race(self) -> None:
        self.record.complete(1, self.LOST, sid=SID)

    def test_a_lost_outcome_writes_no_progress_note(self):
        self.racing = self.race  # at its Progress delivery
        self.lost(TurnEnd(f"{START}\n{block(DONE)}", 0))

    def test_a_lost_give_up_writes_no_notes(self):
        self.call(TurnStarted())
        before = self.kinds()
        self.record.complete(1, self.LOST, sid=SID)
        next(t for t in Timer.made if t.started and not t.cancelled).fire()
        self.assertEqual(self.kinds(), before)
        self.assertEqual(self.kinds_sent(), [])

    def test_notes_of_an_event_that_ends_nothing_are_written(self):
        self.call(TurnStarted())
        self.call(Reply(START))
        self.assertEqual(self.kinds()[-1], "progress")
        self.assertFalse(self.state().completed)


class OwnedCloseTest(HandlerCase):
    """FR-34: a close ends the owned run's active attempt once, as a stop; never an ended one."""

    STOPPED = Result(143, None, "stopped: Stopped: 143")

    def close(self):
        ViewedHarnessDelegate.attach(self.work, session_cls=HarnessSession).close()

    def test_close_ends_an_active_owned_attempt_once_as_stopped(self):
        self.call(TurnStarted())
        self.close()
        state = self.state()
        self.assertEqual((state.attempt, state.completed, state.result), (1, True, self.STOPPED))
        self.assertEqual(self.kinds_sent(), [("finished", 1)])
        finished, at = self.sent[-1]
        self.assertEqual(
            (finished["returncode"], finished["outcome"], finished["error"]),
            (143, None, "stopped: Stopped: 143"),
        )
        self.assertTrue(at["completed"])
        self.assertEqual(self.kinds()[-2:], ["result", "end"])
        self.assertEqual(self.lines(), [["outcome", "error"]])
        self.call(Exited(143))
        self.assertEqual(self.state().result, self.STOPPED)
        self.assertEqual(self.kinds_sent(), [("finished", 1)])

    def test_a_closed_workers_turn_opens_no_follow_up(self):
        self.finish(NEEDS)
        self.close()
        sent = list(self.sent)
        self.call(TurnStarted())
        self.call(Exited(143))
        state = self.state()
        self.assertEqual((state.attempt, state.result.outcome.status), (1, "needs_input"))
        self.assertEqual(self.sent, sent)

    def test_a_close_whose_record_cannot_be_written_still_closes(self):
        self.call(TurnStarted())
        state = self.state()
        spec = replace(state.plan.session, name=state.agent_name)
        HarnessSession.script, HarnessSession.lingers = (), True  # its events: this test's calls
        agent = HarnessSession.start(spec, host=state.host)
        self.assertTrue(agent.alive())
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            self.close()
        self.assertIn("delegate: disk full", self.err.getvalue())
        self.assertFalse(self.state().completed)
        self.assertFalse(agent.alive())
        self.assertEqual(RecordingHost.processes[state.host or "agent", spec.name].closed, 1)

    def test_close_of_an_ended_owned_attempt_sends_nothing(self):
        self.finish(NEEDS)
        before = (replace(self.state(), closing=True), list(self.sent), self.kinds())
        self.close()
        self.call(Exited(0))
        self.assertEqual((self.state(), self.sent, self.kinds()), before)

    def test_close_after_the_handler_ended_the_attempt_sends_nothing(self):
        self.call(TurnStarted())
        stale = self.state()

        class Raced(ViewedHarnessDelegate):
            """Reads the attempt as active: the handler ends it after the close's read."""

            def _read(self):
                return stale

        self.call(TurnEnd(block(DONE), 0))
        Raced.attach(self.work, session_cls=HarnessSession).close()
        self.assertEqual(self.state().result.outcome.status, "done")
        self.assertEqual(self.kinds_sent(), [("finished", 1)])
        self.assertEqual(self.sent[-1][0]["outcome"]["status"], "done")

    def test_close_of_a_run_no_harness_owns_ends_nothing_itself(self):
        state = self.state()
        self.record.save(
            replace(
                state, plan=replace(state.plan, session=replace(state.plan.session, handlers=()))
            )
        )
        self.close()
        self.assertFalse(self.state().completed)
        self.assertEqual(self.sent, [])


class StatusTest(HandlerCase):
    def test_a_draining_owners_view_whose_terminal_is_gone_reads_gone(self):
        self.call(TurnStarted())

        class Gone:
            """A running owner's view whose terminal was killed: its status raises."""

            def alive(self):
                return False

            def status(self):
                raise SessionError(f"session {NAME}: expected sid {SID!r}, found None")

        class Draining(ViewedHarnessDelegate):
            def _agent(self, state):
                return Gone()

        found = Draining.attach(self.work, session_cls=HarnessSession).status()
        self.assertEqual((found.alive, found.state), (False, StatusState.GONE))


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
