"""Persisted callbacks consume real HarnessDelegate events, each delivered to a real recorder child
(tests/delegate/fakes/callback_recorder.py) from the run dir, without affecting the agent
result.
"""

import json
import os
import sys
from dataclasses import asdict
from unittest import mock

from ensemble.delegate import ConfigError, DelegateEvent, Progress, Spec
from ensemble.delegate.engine.state import RunState
from ensemble.delegate.engine.types import Begun, Finished, Layout, Limit
from ensemble.session import ConfigError as SessionConfigError
from ensemble.session import Exited, LimitHit, Reply
from ensemble.utils import Stopped
from tests.delegate.fakes import callback_recorder
from tests.delegate.fakes.harness_session import DrivenDelegate, FailingSession, HarnessSession
from tests.delegate.harness import SID, HarnessCase
from tests.delegate.replies import block, mark
from tests.delegate.unit.runs import DONE

PROGRESS = "回显 one"
OUTCOME = {**DONE, "deliverable": "# 文档\n二\n"}


class CallbackCase(HarnessCase):
    """HarnessCase whose runs deliver to a recorder child logging to self.log."""

    def setUp(self):
        super().setUp()
        HarnessSession.script = (Reply(mark("start", PROGRESS)), Reply(block(OUTCOME)), Exited(0))
        self.log = os.path.join(self.home, "callback.log")

    def callback(self, *rules: str, log=None) -> list[str]:
        return [sys.executable, "-P", "-S", callback_recorder.__file__, log or self.log, *rules]

    def delivered(self, log=None) -> list[dict]:
        try:
            with open(log or self.log, encoding="utf-8") as file:
                return [json.loads(line) for line in file]
        except FileNotFoundError:
            return []

    def events(self, log=None) -> list[DelegateEvent]:
        return [DelegateEvent.parse(entry["line"]) for entry in self.delivered(log)]

    def errors(self) -> list[str]:
        with open(os.path.join(Layout(self.work).run, "run.jsonl"), encoding="utf-8") as file:
            rows = [json.loads(line) for line in file]
        return [row["error"] for row in rows if row["kind"] == "callback_error"]


class Callbacks(CallbackCase):
    def test_each_event_is_one_stdin_line_in_order_from_the_run_dir(self):
        delegate = self.make(callback=self.callback())
        result = delegate.run()
        self.assertEqual(self.metadata()["callback"], self.callback())
        entries = self.delivered()
        for entry in entries:
            self.assertEqual(entry["line"].count("\n"), 1)
            self.assertTrue(entry["line"].endswith("\n"))
            self.assertEqual(entry["cwd"], os.path.realpath(Layout(self.work).run))
            self.assertEqual(entry["pgid"], entry["pid"])
            self.assertEqual(
                (entry["held"], entry["lock"]), (True, f"{entry['pid']} {os.getpid()}\n")
            )
        store = RunState(Layout(self.work))
        self.assertFalse(store.held(store.callback_lock))
        events = self.events()
        self.assertEqual([event.kind for event in events], ["begun", "progress", "finished"])
        self.assertEqual({(event.sid, event.attempt) for event in events}, {(SID, 1)})
        self.assertEqual((events[1].name, events[1].text), ("start", PROGRESS))
        self.assertEqual(
            (events[-1].returncode, events[-1].error, events[-1].limit),
            (result.returncode, result.error, None),
        )
        self.assertEqual(
            asdict(events[-1].outcome),
            {**OUTCOME, "questions": [], "url": Layout(self.work).out, "files": []},
        )
        self.assertEqual(self.errors(), [])

    def test_a_failed_delivery_is_one_callback_error_row_and_later_events_still_arrive(self):
        missing = os.path.join(self.home, "missing-callback")
        for index, (rules, command, kinds, errors) in enumerate(
            (
                (("--exit", "begun:1"), None, ["begun", "progress", "finished"], 1),
                ((), [missing], [], 3),
            )
        ):
            with self.subTest(rules=rules, command=command):
                self.work = os.path.join(self.home, f"callback-failure-{index}")
                log = os.path.join(self.home, f"callback-failure-{index}.log")
                result = self.make(callback=command or self.callback(*rules, log=log)).run()
                self.assertEqual(
                    (result.returncode, result.outcome.status if result.outcome else None),
                    (0, "done"),
                )
                self.assertEqual([event.kind for event in self.events(log)], kinds)
                self.assertEqual(len(self.errors()), errors)
                expected = "callback exited 1" if command is None else missing
                self.assertTrue(all(expected in error for error in self.errors()))

    def test_the_harness_side_attach_delivers_to_the_persisted_argv(self):
        self.make(callback=self.callback())
        DrivenDelegate.attach(self.work, session_cls=HarnessSession).run()
        self.assertEqual([event.kind for event in self.events()], ["begun", "progress", "finished"])

    def test_a_replacing_run_stamps_its_own_attempt(self):
        self.make().run()
        replacing = self.make(callback=self.callback())
        replacing.run()
        self.assertEqual({(event.sid, event.attempt) for event in self.events()}, {(SID, 2)})

    def test_a_new_session_replacing_an_ended_run_is_attempt_1(self):
        self.make().run()
        other = "1f8fad5b-d9cb-469f-a165-70867728950e"
        spec = Spec("Hello.", self.work, task="echo", sid=other, client="fake")
        self.make(spec=spec, callback=self.callback()).run()
        self.assertEqual(self.metadata()["attempt"], 1)
        self.assertEqual({(event.sid, event.attempt) for event in self.events()}, {(other, 1)})

    def test_no_callback_invokes_no_command(self):
        with mock.patch("subprocess.Popen") as popen:
            result = self.make().run()
        self.assertEqual(result.returncode, 0)
        popen.assert_not_called()


class RefusedSession(HarnessSession):
    @classmethod
    def start(cls, spec, *, display=None, host=None, idle_timeout=None):
        raise SessionConfigError("fake start refused")


class Raising:
    """An on_event noting each event, raising `error` on the first event of type `on`."""

    def __init__(self, error, on):
        self.error, self.on, self.seen = error, on, []

    def __call__(self, event):
        self.seen.append(event)
        if isinstance(event, self.on):
            raise self.error


class OneFinished(CallbackCase):
    def finished(self, event: DelegateEvent) -> tuple:
        return (event.returncode, event.outcome, event.error, event.limit)

    def test_every_returned_result_delivers_exactly_one_last_finished(self):
        for index, (script, session_cls) in enumerate(
            (
                ((Reply(block(DONE)), Exited(0)), HarnessSession),
                ((Reply("no outcome"), Exited(0)), HarnessSession),
                ((Exited(7),), HarnessSession),
                ((LimitHit(1767225600.0), Exited(0)), HarnessSession),
                ((), FailingSession),
            )
        ):
            with self.subTest(script=script, session_cls=session_cls):
                self.work = os.path.join(self.home, f"returned-{index}")
                log = os.path.join(self.home, f"returned-{index}.log")
                HarnessSession.script = script
                result = self.make(callback=self.callback(log=log), session_cls=session_cls).run()
                events = self.events(log)
                kinds = [event.kind for event in events]
                self.assertEqual(kinds.count("finished"), 1)
                self.assertEqual(kinds[-1], "finished")
                self.assertEqual(
                    self.finished(events[-1]),
                    (
                        result.returncode,
                        result.outcome,
                        result.error,
                        None if result.limit is None else {"resets_at": result.limit.resets_at},
                    ),
                )
                if script and isinstance(script[0], LimitHit):
                    self.assertEqual(result.limit, Limit(1767225600.0))
                if session_cls is FailingSession:
                    self.assertEqual(kinds, ["finished"])

    def test_a_raised_run_sends_one_stopped_finished_to_the_callback_and_the_stored_result(self):
        for index, (error, stopped) in enumerate(
            (
                (RuntimeError("boom\n  now"), (1, "stopped: RuntimeError: boom now")),
                (Stopped(143), (143, "stopped: Stopped: 143")),
            )
        ):
            with self.subTest(error=error):
                self.work = os.path.join(self.home, f"raised-{index}")
                log = os.path.join(self.home, f"raised-{index}.log")
                on_event = Raising(error, Progress)
                with self.assertRaises(type(error)) as raised:
                    self.make(callback=self.callback(log=log)).run(on_event)
                self.assertIs(raised.exception, error)
                events = self.events(log)
                self.assertEqual(
                    [event.kind for event in events], ["begun", "progress", "finished"]
                )
                self.assertEqual(self.finished(events[-1]), (*stopped[:1], None, stopped[1], None))
                self.assertEqual([type(event) for event in on_event.seen], [Begun, Progress])
                stored = self.metadata()
                self.assertTrue(stored["completed"])
                self.assertEqual(
                    stored["result"],
                    {
                        "returncode": stopped[0],
                        "outcome": None,
                        "error": stopped[1],
                        "limit": None,
                        "instruction": None,
                    },
                )
                with open(
                    os.path.join(Layout(self.work).run, "run.jsonl"), encoding="utf-8"
                ) as file:
                    crashes = [row for row in map(json.loads, file) if row["kind"] == "run_error"]
                if isinstance(error, Stopped):
                    self.assertEqual(crashes, [])
                else:
                    [crash] = crashes
                    self.assertEqual(crash["error"], stopped[1])
                    self.assertTrue(crash["traceback"].startswith("Traceback"), crash)
                    self.assertTrue(crash["traceback"].endswith("RuntimeError: boom\n  now\n"))

    def test_an_on_event_raising_on_finished_leaves_the_callbacks_one_real_finished(self):
        failure = RuntimeError("observer failed")
        with self.assertRaises(RuntimeError) as raised:
            self.make(callback=self.callback()).run(Raising(failure, Finished))
        self.assertIs(raised.exception, failure)
        events = self.events()
        self.assertEqual([event.kind for event in events], ["begun", "progress", "finished"])
        self.assertEqual(events[-1].outcome.status, "done")
        self.assertEqual(self.metadata()["result"]["outcome"]["status"], "done")

    def test_a_refused_start_sends_one_stopped_finished(self):
        spec = Spec("input", self.work, client="fake")
        delegate = DrivenDelegate(
            "dummy-tester", spec, session_cls=RefusedSession, callback=self.callback()
        )
        with self.assertRaises(ConfigError):
            delegate.run()
        (event,) = self.events()
        self.assertEqual(
            self.finished(event), (1, None, "stopped: ConfigError: fake start refused", None)
        )
