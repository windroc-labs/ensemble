"""harness/process.py: the session harness that owns one worker, logging each event before its
handlers get it, serving close and stop, and draining its handlers after the worker's exit.
"""

import json
import os
import threading
import time
from types import SimpleNamespace
from unittest import mock

from ensemble.session.engine.types import (
    Blocked,
    ClientBound,
    ConfigError,
    Exited,
    Reply,
    SessionBusy,
    SessionError,
    Stderr,
    TurnEnd,
    TurnFailed,
    TurnStarted,
    Unanswered,
)
from ensemble.session.harness.channel import Channel
from ensemble.session.harness.process import Harness, Turns
from ensemble.session.harness.types import Op
from tests.session.case import Case

GEN = "g1"
WAIT = 5


class FakeOwned:
    """Scripted: each pump returns the next (events, ended) step, then nothing; stop and close return
    `final`; confirm_end returns `code` (None: unconfirmed).
    """

    remote = False

    def __init__(self, *steps, final=(), code=0):
        self.name, self.sid, self.generation = "w1", "s1", GEN
        self.steps, self.final, self.code = list(steps), list(final), code
        self.calls, self.recorded = [], []
        self.ended = False
        self.opening_pending = None

    def pump(self, wait):
        if self.steps:
            found, self.ended = self.steps.pop(0)
            return list(found), self.ended
        time.sleep(min(wait, 0.01))
        return [], self.ended

    def record(self, event):
        self.recorded.append(event)

    def stop(self):
        self.calls.append("stop")
        return self.final

    def close(self):
        self.calls.append("close")
        return self.final

    def confirm_end(self, timeout):
        self.calls.append("confirm_end")
        return self.code

    def returncode(self):
        return self.code

    def view(self, channel, idle_timeout=None):
        return SimpleNamespace(name="view")


class Handler:
    def __init__(self, *, fail=False, hang=None, on=None):
        self.calls, self.fail, self.hang, self.on = [], fail, hang, on

    def on_event(self, call):
        self.calls.append(call)
        if self.on is not None:
            self.on(call)
        if self.hang is not None:
            self.hang.wait()
        if self.fail:
            raise RuntimeError("broke")


class Base(Case):
    def setUp(self):
        self.channel = Channel.create()
        self.channel.write_request({"spec": {}})
        self.channel.hold()
        self.addCleanup(self.channel.release)

    def harness(self, owned, *handlers, drain=1.0, close_wait=0.1) -> Harness:
        harness = Harness(self.channel, poll=0.01, drain=drain, close_wait=close_wait)
        self.begin = mock.Mock(return_value=(owned, list(handlers), [{}] * len(handlers)))
        return harness

    def run_in_thread(self, harness) -> threading.Thread:
        self.code = None

        def run():
            self.code = harness.run(self.begin)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, WAIT)
        return thread

    def ask(self, op, data):
        return self.channel.ask(op, data, timeout=WAIT)

    def running(self, owned, *handlers, **kw):
        thread = self.run_in_thread(self.harness(owned, *handlers, **kw))
        while self.channel.status().get("state") != "running":
            time.sleep(0.01)
        return thread

    def logged(self) -> list[dict]:
        if not os.path.exists(self.channel.events):
            return []
        with open(self.channel.events, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def types(self) -> list[str]:
        return [record["type"] for record in self.logged()]

    def request(self, op) -> bool:
        return self.channel.request(op, timeout=WAIT)


class TurnsTest(Case):
    def test_turn_ids(self):
        turns = Turns(GEN)
        self.assertEqual(turns.assign(Reply("x")), [(None, Reply("x"))])
        self.assertEqual(turns.assign(TurnStarted()), [("g1:t1", TurnStarted())])
        self.assertEqual(turns.assign(Blocked("Bash")), [("g1:t1", Blocked("Bash"))])
        self.assertEqual(turns.assign(TurnEnd("r", 0)), [("g1:t1", TurnEnd("r", 0))])
        self.assertEqual(turns.assign(Stderr("e")), [(None, Stderr("e"))])

    def test_a_terminal_event_without_a_start_has_a_turn_of_its_own(self):
        turns = Turns(GEN)
        self.assertEqual(turns.assign(TurnFailed("e", "")), [("g1:t1", TurnFailed("e", ""))])
        self.assertEqual(turns.assign(TurnEnd("r", 0)), [("g1:t2", TurnEnd("r", 0))])

    def test_a_missed_end_is_superseded(self):
        turns = Turns(GEN)
        turns.assign(TurnStarted())
        self.assertEqual(
            turns.assign(TurnStarted()),
            [("g1:t1", TurnFailed("superseded", "")), ("g1:t2", TurnStarted())],
        )

    def test_an_exit_mid_turn_fails_it(self):
        turns = Turns(GEN)
        self.assertEqual(turns.exited(0), [])
        turns.assign(TurnStarted())
        self.assertEqual(
            turns.exited(3), [("g1:t1", TurnFailed("exited", "worker exited 3 mid-turn"))]
        )
        self.assertEqual(turns.exited(3), [])


class RecordThenDispatchTest(Base):
    def test_each_event_is_logged_with_its_ids_before_its_handlers_get_it(self):
        seen = []

        def on(call):
            seen.append((call.event_id, [r["id"] for r in self.logged()]))

        handler = Handler(on=on)
        owned = FakeOwned(
            ([TurnStarted(), ClientBound("c1"), Blocked("Bash")], False),
            ([Stderr("e"), TurnEnd("full\nreply", 1)], False),
            ([], True),
        )
        self.assertEqual(self.harness(owned, handler).run(self.begin), 0)
        self.assertEqual(
            [(r["seq"], r["id"], r["turn"], r["type"]) for r in self.logged()],
            [
                (1, "g1:1", "g1:t1", "TurnStarted"),
                (2, "g1:2", "g1:t1", "ClientBound"),
                (3, "g1:3", "g1:t1", "Blocked"),
                (4, "g1:4", "g1:t1", "Stderr"),
                (5, "g1:5", "g1:t1", "TurnEnd"),
                (6, "g1:6", None, "Exited"),
            ],
        )
        self.assertEqual(
            [c.event for c in handler.calls],
            [TurnStarted(), Blocked("Bash"), TurnEnd("full\nreply", 1), Exited(0)],
        )
        for event_id, ids in seen:
            self.assertIn(event_id, ids)
        self.assertEqual(owned.recorded[:3], [TurnStarted(), ClientBound("c1"), Blocked("Bash")])
        status = self.channel.status()
        self.assertEqual(
            {k: status[k] for k in ("state", "sid", "name", "generation", "returncode", "pending")},
            {
                "state": "exited",
                "sid": "s1",
                "name": "w1",
                "generation": GEN,
                "returncode": 0,
                "pending": 0,
            },
        )
        self.assertEqual(status["pid"], os.getpid())
        self.assertFalse(os.path.exists(os.path.join(self.channel.directory, "request.json")))
        self.begin.assert_called_once_with({"spec": {}})

    def test_the_handlers_session_is_the_owners_view(self):
        handler = Handler()
        self.harness(FakeOwned(([Reply("x")], True)), handler).run(self.begin)
        self.assertEqual(handler.calls[0].session.name, "view")


class StartErrorTest(Base):
    def test_a_start_error_is_written_as_failed(self):
        for error, expect in (
            (ConfigError("bad handler"), {"type": "ConfigError", "message": "bad handler"}),
            (SessionError("no tmux"), {"type": "SessionError", "message": "no tmux"}),
            (
                SessionBusy("L", "c (owner o)"),
                {
                    "type": "SessionBusy",
                    "message": "busy: lock L is held by c (owner o)",
                    "lock": "L",
                    "holder": "c (owner o)",
                },
            ),
        ):
            with self.subTest(error=type(error).__name__):
                if not os.path.exists(os.path.join(self.channel.directory, "request.json")):
                    self.channel.write_request({"spec": {}})
                harness = Harness(self.channel, poll=0.01)
                self.assertEqual(harness.run(mock.Mock(side_effect=error)), 1)
                status = self.channel.status()
                self.assertEqual((status["state"], status["error"]), ("failed", expect))
                self.assertEqual(self.logged(), [])


class CloseRequestTest(Base):
    def test_a_turn_end_written_before_the_close_is_logged_before_exited(self):
        handler = Handler()
        owned = FakeOwned(([TurnStarted()], False), final=[TurnEnd("last", 0)], code=-1)
        self.run_in_thread(self.harness(owned, handler))
        while "TurnStarted" not in self.types():
            time.sleep(0.01)
        self.assertTrue(self.request(Op.CLOSE))
        self.assertEqual(self.types(), ["TurnStarted", "TurnEnd", "Exited"])
        self.assertEqual(owned.calls, ["close", "confirm_end"])
        self.assertEqual(self.logged()[-1]["fields"], {"returncode": -1})

    def test_stop_is_the_owners_stop(self):
        owned = FakeOwned(code=0)
        self.run_in_thread(self.harness(owned))
        self.assertTrue(self.request(Op.STOP))
        self.assertEqual(owned.calls[0], "stop")

    def test_a_close_mid_turn_fails_the_turn_first(self):
        owned = FakeOwned(([TurnStarted()], False), code=-1)
        self.run_in_thread(self.harness(owned))
        while "TurnStarted" not in self.types():
            time.sleep(0.01)
        self.assertTrue(self.request(Op.CLOSE))
        self.assertEqual(self.types(), ["TurnStarted", "TurnFailed", "Exited"])

    def test_an_unconfirmed_end_is_answered_unanswered_and_supervision_goes_on(self):
        owned = FakeOwned(code=None)
        thread = self.run_in_thread(self.harness(owned))
        with self.assertRaisesRegex(SessionError, "not confirmed") as caught:
            self.request(Op.CLOSE)
        self.assertEqual(type(caught.exception).__name__, "Unanswered")
        self.assertEqual(self.channel.status()["state"], "running")
        self.assertFalse(self.channel.status()["remote"])
        owned.code = 0
        self.assertTrue(self.request(Op.CLOSE))
        thread.join(WAIT)
        self.assertEqual(self.code, 0)

    def test_close_is_served_while_a_handler_hangs(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        owned = FakeOwned(([Reply("x")], False))
        thread = self.run_in_thread(self.harness(owned, Handler(hang=gate), drain=0.2))
        while "Reply" not in self.types():
            time.sleep(0.01)
        self.assertTrue(self.request(Op.CLOSE))
        thread.join(WAIT)
        self.assertEqual(self.code, 0)
        self.assertEqual(self.channel.status()["pending"], 2)

    def test_a_signal_closes_like_a_request(self):
        owned = FakeOwned()
        harness = self.harness(owned)
        thread = self.run_in_thread(harness)
        while self.channel.status().get("state") != "running":
            time.sleep(0.01)
        harness.signalled(15, None)
        thread.join(WAIT)
        self.assertEqual((self.code, owned.calls[0], self.types()), (0, "close", ["Exited"]))


class ExitTest(Base):
    def test_an_exit_mid_turn_fails_the_turn_then_exits_once(self):
        owned = FakeOwned(([TurnStarted()], False), ([Reply("partial")], True), code=3)
        self.assertEqual(self.harness(owned).run(self.begin), 0)
        records = self.logged()
        self.assertEqual(
            [(r["type"], r["turn"]) for r in records],
            [
                ("TurnStarted", "g1:t1"),
                ("Reply", "g1:t1"),
                ("TurnFailed", "g1:t1"),
                ("Exited", None),
            ],
        )
        self.assertEqual(
            records[2]["fields"], {"error": "exited", "detail": "worker exited 3 mid-turn"}
        )
        self.assertEqual(owned.calls, [])

    def test_exit_with_a_hung_handler_exits_0_with_pending(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        owned = FakeOwned(([Reply("x")], True))
        started = time.monotonic()
        self.assertEqual(self.harness(owned, Handler(hang=gate), drain=0.2).run(self.begin), 0)
        self.assertLess(time.monotonic() - started, WAIT)
        status = self.channel.status()
        self.assertEqual((status["state"], status["pending"]), ("exited", 2))


class DrainTest(Base):
    def test_requests_during_the_drain_are_answered_ok(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        owned = FakeOwned(([Reply("x")], True))
        thread = self.run_in_thread(self.harness(owned, Handler(hang=gate), drain=WAIT))
        while self.channel.status().get("state") != "exited":
            time.sleep(0.01)
        self.assertTrue(self.request(Op.CLOSE))
        self.assertTrue(self.request(Op.STOP))
        gate.set()
        thread.join(WAIT)
        self.assertEqual(owned.calls, ["close", "stop"])
        self.assertEqual(self.types(), ["Reply", "Exited"])

    def test_a_failed_close_during_the_drain_is_answered_with_its_error(self):
        owned = FakeOwned(([], True))
        owned.close = mock.Mock(side_effect=SessionError("cannot close w1"))
        gate = threading.Event()
        self.addCleanup(gate.set)
        thread = self.run_in_thread(self.harness(owned, Handler(hang=gate), drain=WAIT))
        while self.channel.status().get("state") != "exited":
            time.sleep(0.01)
        with self.assertRaisesRegex(SessionError, "cannot close w1"):
            self.request(Op.CLOSE)
        gate.set()
        thread.join(WAIT)

    def test_a_handler_closing_on_exited_returns(self):
        results = []

        def on(call):
            if isinstance(call.event, Exited):
                results.append(self.channel.request(Op.CLOSE, timeout=WAIT))

        owned = FakeOwned(([], True))
        self.assertEqual(self.harness(owned, Handler(on=on), drain=WAIT).run(self.begin), 0)
        self.assertEqual(results, [True])


class FailureTest(Base):
    def test_an_unexpected_error_closes_the_worker_and_fails(self):
        owned = FakeOwned()
        owned.pump = mock.Mock(side_effect=RuntimeError("bug"))
        with self.assertLogs("session", "ERROR"):
            self.assertEqual(self.harness(owned).run(self.begin), 1)
        self.assertEqual(owned.calls, ["close", "confirm_end"])
        self.assertEqual(self.types(), ["Exited"])
        self.assertEqual(self.channel.status()["state"], "failed")


class OpeningTest(Base):
    def test_its_pending_opening_is_in_its_status(self):
        owned = FakeOwned(([], False), ([], False))
        owned.opening_pending = "/color"
        thread = self.run_in_thread(self.harness(owned))
        while self.channel.status().get("opening") != "/color":
            time.sleep(0.01)
        owned.opening_pending = None
        while self.channel.status().get("opening") is not None:
            time.sleep(0.01)
        owned.ended = True
        thread.join(WAIT)
        self.assertEqual(self.code, 0)


class BatchTest(Base):
    def test_requests_taken_with_the_one_that_ends_the_worker_are_still_answered(self):
        folder = os.path.join(self.channel.directory, "requests")
        os.makedirs(folder)
        idents = ["a" * 32, "b" * 32]
        for ident, op in zip(idents, ("stop", "close"), strict=True):
            with open(os.path.join(folder, ident + ".json"), "w") as f:
                f.write(json.dumps({"op": op}))
        owned = FakeOwned()
        self.assertEqual(self.harness(owned).run(self.begin), 0)
        self.assertEqual(owned.calls, ["stop", "confirm_end", "close"])
        for ident in idents:
            with open(os.path.join(self.channel.directory, "replies", ident + ".json")) as f:
                self.assertEqual(json.load(f), {"ok": True})
        self.assertEqual(self.types(), ["Exited"])


class RemoteOwned(FakeOwned):
    remote = True

    def __init__(self, *steps, **kw):
        super().__init__(*steps, **kw)
        self.inputs = []
        self.failure = None

    def send(self, text):
        self._did(("send", text))

    def press(self, keys):
        self._did(("press", [str(key) for key in keys]))

    def screen(self, lines=None):
        self._did(("screen", lines))
        return f"screen {lines}"

    def _did(self, call):
        if self.failure is not None:
            raise self.failure
        self.inputs.append(call)


class DisplayOwned(FakeOwned):
    """A local worker whose display redisplay opens again: `reopened`, else raises `failure`."""

    def __init__(self, *steps, **kw):
        super().__init__(*steps, **kw)
        self.reopened, self.failure, self.asked = True, None, 0

    def redisplay(self):
        self.asked += 1
        if self.failure is not None:
            raise self.failure
        return self.reopened


class RemoteTest(Base):
    def test_send_press_and_screen_are_served_and_the_worker_stays_up(self):
        owned = RemoteOwned()
        self.running(owned)
        self.assertTrue(self.channel.status()["remote"])
        self.assertEqual(self.ask(Op.SEND, {"text": "hello"}), {})
        self.assertEqual(self.ask(Op.PRESS, {"keys": ["Enter", "C-c"]}), {})
        self.assertEqual(self.ask(Op.SCREEN, {"lines": 3}), {"text": "screen 3"})
        self.assertEqual(self.ask(Op.SCREEN, {"lines": None}), {"text": "screen None"})
        self.assertEqual(
            owned.inputs,
            [("send", "hello"), ("press", ["Enter", "C-c"]), ("screen", 3), ("screen", None)],
        )
        self.assertEqual(owned.calls, [])
        self.assertEqual(self.channel.status()["state"], "running")
        self.assertTrue(self.request(Op.CLOSE))

    def test_bad_fields_and_worker_errors_are_refused_and_the_worker_stays_up(self):
        owned = RemoteOwned()
        self.running(owned)
        for op, data in (
            (Op.SEND, {"text": 5}),
            (Op.SEND, {}),
            (Op.SEND, {"text": "x" * ((1 << 20) + 1)}),
            (Op.SEND, {"text": "\ud800"}),
            (Op.PRESS, {"keys": []}),
            (Op.PRESS, {"keys": ["Enter"] * 65}),
            (Op.PRESS, {"keys": ["no such key"]}),
            (Op.PRESS, {"keys": [5]}),
            (Op.PRESS, {"keys": "Enter"}),
            (Op.SCREEN, {"lines": True}),
            (Op.SCREEN, {"lines": 0}),
            (Op.SCREEN, {"lines": 10001}),
            (Op.SCREEN, {"lines": "3"}),
            (Op.SCREEN, {}),
        ):
            with self.subTest(op=op, data=str(data)[:40]), self.assertRaises(SessionError):
                self.ask(op, data)
        owned.failure = Unanswered("worker w1: screen unanswered")
        with self.assertRaises(Unanswered):
            self.ask(Op.SCREEN, {"lines": 1})
        for failure in (ConfigError("lines: bad"), SessionError("tui session w1 is not running")):
            owned.failure = failure
            with self.subTest(failure=failure), self.assertRaisesRegex(SessionError, str(failure)):
                self.ask(Op.SEND, {"text": "x"})
        self.assertEqual(owned.inputs, [])
        self.assertEqual(self.channel.status()["state"], "running")
        self.assertEqual(owned.calls, [])
        self.assertTrue(self.request(Op.CLOSE))

    def test_after_the_exit_input_is_refused_not_a_close(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        owned = RemoteOwned(([Reply("x")], True))
        thread = self.run_in_thread(self.harness(owned, Handler(hang=gate), drain=2.0))
        while self.channel.status().get("state") != "exited":
            time.sleep(0.01)
        with self.assertRaisesRegex(SessionError, "w1: worker exited"):
            self.ask(Op.SEND, {"text": "late"})
        gate.set()
        thread.join(WAIT)
        self.assertEqual(owned.inputs, [])
        self.assertNotIn("close", owned.calls)


class DisplayTest(Base):
    def test_display_is_served_and_the_worker_stays_up(self):
        owned = DisplayOwned()
        self.running(owned)
        self.assertEqual(self.ask(Op.DISPLAY, {}), {"reopened": True})
        owned.reopened = False
        self.assertEqual(self.ask(Op.DISPLAY, {}), {"reopened": False})
        self.assertEqual(owned.asked, 2)
        self.assertEqual(owned.calls, [])
        self.assertEqual(self.channel.status()["state"], "running")
        self.assertTrue(self.request(Op.CLOSE))

    def test_a_display_failure_is_refused_and_the_worker_stays_up(self):
        owned = DisplayOwned()
        self.running(owned)
        owned.failure = SessionError("worker w1: its name shows another display")
        with self.assertRaisesRegex(SessionError, "^worker w1: its name shows another display$"):
            self.ask(Op.DISPLAY, {})
        owned.failure = Unanswered("worker w1: its display unknown")
        with self.assertRaises(Unanswered):
            self.ask(Op.DISPLAY, {})
        self.assertEqual(owned.calls, [])
        self.assertEqual(self.channel.status()["state"], "running")
        self.assertTrue(self.request(Op.CLOSE))

    def test_after_the_exit_display_is_refused(self):
        owned = DisplayOwned(([Reply("x")], True))
        thread = self.run_in_thread(self.harness(owned, drain=0.5))
        while self.channel.status().get("state") != "exited":
            time.sleep(0.01)
        with self.assertRaisesRegex(SessionError, "w1: worker exited"):
            self.ask(Op.DISPLAY, {})
        thread.join(WAIT)
        self.assertEqual(owned.asked, 0)
