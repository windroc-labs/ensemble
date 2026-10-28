"""harness/dispatch.py: handler modules found and loaded by their on_event entry, and each logged
event handed to every handler in order, a failure retried with the same ids.
"""

import os
import sys
import threading
import time
import types
import uuid
from typing import cast
from unittest import mock

from ensemble.session.engine.base import Session
from ensemble.session.engine.types import (
    ClientBound,
    ConfigError,
    Exited,
    HookCallback,
    Reply,
    Stderr,
    TurnEnd,
    TurnStarted,
)
from ensemble.session.harness import dispatch
from ensemble.session.harness.channel import Channel
from ensemble.session.harness.dispatch import Dispatch, load_handlers
from ensemble.session.harness.types import Logged
from tests.session.case import Case
from tests.session.harnesses import until
from tests.utils.jsonl import read_jsonl

WAIT = 5


class Recorder:
    """A handler module: on_event records each call; `fail` raises on its first `fail` calls."""

    def __init__(self, *, fail=0, hang=None, returns=None):
        self.calls, self.fail, self.hang, self.returns = [], fail, hang, returns
        self.lock = threading.Lock()

    def on_event(self, call):
        with self.lock:
            self.calls.append(call)
            failing = self.fail > 0
            self.fail -= failing
        if self.hang is not None:
            self.hang.wait()
        if failing:
            raise RuntimeError("handler broke")
        return self.returns


class Base(Case):
    def setUp(self):
        self.channel = Channel.create()
        self.session = cast(Session, object())
        self.sleeps = []

    def start(self, *handlers, sleep=None) -> Dispatch:
        found = Dispatch(
            handlers,
            [{"n": index} for index in range(len(handlers))],
            self.session,
            self.channel,
            sid="s1",
            generation="g",
            sleep=sleep or self.sleeps.append,
        )
        self.addCleanup(found.stop)
        return found

    def deliveries(self) -> list[dict]:
        return read_jsonl(self.channel.deliveries, missing_ok=True)


class DispatchTest(Base):
    def test_each_handler_gets_each_event_in_order_with_its_ids(self):
        first, second = Recorder(), Recorder()
        found = self.start(first, second)
        found.put(Logged(1, "g:1", "g:t1", TurnStarted()))
        found.put(Logged(2, "g:2", "g:t1", TurnEnd("full\nreply", 0)))
        found.put(Logged(3, "g:3", None, Exited(0)))
        self.assertEqual(found.drain(WAIT), 0)
        for index, handler in enumerate((first, second)):
            with self.subTest(handler=index):
                self.assertEqual(
                    [(c.event_id, c.turn, c.event) for c in handler.calls],
                    [
                        ("g:1", "g:t1", TurnStarted()),
                        ("g:2", "g:t1", TurnEnd("full\nreply", 0)),
                        ("g:3", None, Exited(0)),
                    ],
                )
                call = handler.calls[0]
                self.assertIs(call.session, self.session)
                self.assertEqual((call.sid, call.generation, call.state), ("s1", "g", {"n": index}))
        self.assertEqual(
            sorted((d["handler"], d["id"], d["attempt"], d["state"]) for d in self.deliveries()),
            sorted((h, f"g:{n}", 1, "ok") for h in (0, 1) for n in (1, 2, 3)),
        )

    def test_client_bound_and_stderr_are_never_dispatched(self):
        handler = Recorder()
        found = self.start(handler)
        found.put(Logged(1, "g:1", None, ClientBound("c1")))
        found.put(Logged(2, "g:2", None, Stderr("oops")))
        found.put(Logged(3, "g:3", None, Reply("hi")))
        self.assertEqual(found.drain(WAIT), 0)
        self.assertEqual([c.event for c in handler.calls], [Reply("hi")])

    def test_a_failing_handler_is_retried_with_the_same_ids(self):
        self.enterContext(self.assertLogs("session", "WARNING"))
        handler = Recorder(fail=2)
        found = self.start(handler)
        found.put(Logged(1, "g:1", "g:t1", TurnStarted()))
        self.assertEqual(found.drain(WAIT), 0)
        self.assertEqual([(c.event_id, c.turn) for c in handler.calls], [("g:1", "g:t1")] * 3)
        self.assertEqual(self.sleeps, [1, 2])
        records = self.deliveries()
        self.assertEqual(
            [(d["attempt"], d["state"]) for d in records], [(1, "failed"), (2, "failed"), (3, "ok")]
        )
        self.assertIn("handler broke", records[0]["error"])

    def test_backoff_doubles_up_to_thirty_seconds(self):
        """Only attempts 1, 2, 4 and so on are noted."""
        self.enterContext(self.assertLogs("session", "WARNING"))
        found = self.start(Recorder(fail=8))
        found.put(Logged(1, "g:1", None, Reply("x")))
        self.assertEqual(found.drain(WAIT), 0)
        self.assertEqual(self.sleeps, [1, 2, 4, 8, 16, 30, 30, 30])
        self.assertEqual(
            [(d["attempt"], d["state"]) for d in self.deliveries()],
            [(1, "failed"), (2, "failed"), (4, "failed"), (8, "failed"), (9, "ok")],
        )

    def test_a_handler_that_exits_is_retried_like_any_failure(self):
        class Exits(Recorder):
            def on_event(self, call):
                self.calls.append(call)
                if len(self.calls) == 1:
                    raise SystemExit(3)

        self.enterContext(self.assertLogs("session", "WARNING"))
        handler = Exits()
        found = self.start(handler)
        found.put(Logged(1, "g:1", None, Reply("x")))
        found.put(Logged(2, "g:2", None, Reply("y")))
        self.assertEqual(found.drain(WAIT), 0)
        self.assertEqual([c.event_id for c in handler.calls], ["g:1", "g:1", "g:2"])

    def test_error_text_is_capped(self):
        class Loud(Recorder):
            def on_event(self, call):
                if not self.calls:
                    self.calls.append(call)
                    raise RuntimeError("x" * 5000)

        self.enterContext(self.assertLogs("session", "WARNING"))
        found = self.start(Loud())
        found.put(Logged(1, "g:1", None, Reply("x")))
        until(lambda: self.deliveries())
        self.assertEqual(len(self.deliveries()[0]["error"]), dispatch.ERROR_MAX)

    def test_later_events_wait_behind_a_failure(self):
        self.enterContext(self.assertLogs("session", "WARNING"))
        gate = threading.Event()
        handler = Recorder(fail=1)
        found = self.start(handler, sleep=lambda _seconds: gate.wait())
        found.put(Logged(1, "g:1", None, Reply("a")))
        found.put(Logged(2, "g:2", None, Reply("b")))
        until(lambda: self.deliveries())
        self.assertEqual([c.event_id for c in handler.calls], ["g:1"])
        self.assertEqual(found.pending(), 2)
        gate.set()
        self.assertEqual(found.drain(WAIT), 0)
        self.assertEqual([c.event_id for c in handler.calls], ["g:1", "g:1", "g:2"])

    def test_other_handlers_continue(self):
        self.enterContext(self.assertLogs("session", "WARNING"))
        gate = threading.Event()
        self.addCleanup(gate.set)
        stuck, other = Recorder(fail=1), Recorder()
        found = self.start(stuck, other, sleep=lambda _seconds: gate.wait())
        found.put(Logged(1, "g:1", None, Reply("a")))
        found.put(Logged(2, "g:2", None, Reply("b")))
        until(lambda: len(other.calls) == 2)
        self.assertEqual(found.pending(), 2)

    def test_a_hung_handler_blocks_nothing_else(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        hung, other = Recorder(hang=gate), Recorder()
        found = self.start(hung, other)
        found.put(Logged(1, "g:1", None, Reply("a")))
        until(lambda: len(other.calls) == 1)
        started = time.monotonic()
        self.assertEqual(found.drain(0.1), 1)
        self.assertLess(time.monotonic() - started, WAIT)
        self.assertTrue(all(thread.daemon for thread in found.threads))

    def woken_after_stop(self, hung: Recorder, gate: threading.Event) -> Dispatch:
        found = self.start(hung)
        found.put(Logged(1, "g:1", None, Reply("a")))
        until(lambda: len(hung.calls) == 1)
        found.stop()
        gate.set()
        for thread in found.threads:
            thread.join(WAIT)
        return found

    def test_a_hung_handler_woken_after_stop_records_nothing(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        found = self.woken_after_stop(Recorder(hang=gate), gate)
        self.assertEqual((self.deliveries(), found.pending()), ([], 1))

    def test_a_hung_handler_failing_after_stop_records_nothing(self):
        self.enterContext(self.assertLogs("session", "WARNING"))
        gate = threading.Event()
        self.addCleanup(gate.set)
        found = self.woken_after_stop(Recorder(fail=1, hang=gate), gate)
        self.assertEqual((self.deliveries(), found.pending()), ([], 1))

    def test_a_note_being_written_ends_before_stop_returns(self):
        entered, release, stopping = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        note = self.channel.note

        def blocked(record):
            entered.set()
            release.wait(WAIT)
            note(record)

        self.channel.note = blocked
        found = self.start(Recorder())
        found.put(Logged(1, "g:1", None, Reply("a")))
        self.assertTrue(entered.wait(WAIT))
        seen = []

        def stop():
            stopping.set()
            found.stop()
            seen.append(self.deliveries())

        stopper = threading.Thread(target=stop)
        stopper.start()
        self.assertTrue(stopping.wait(WAIT))
        stopper.join(0.2)
        self.assertTrue(stopper.is_alive())
        release.set()
        stopper.join(WAIT)
        for thread in found.threads:
            thread.join(WAIT)
        self.assertEqual(seen, [self.deliveries()])
        self.assertEqual(len(seen[0]), 1)

    def test_a_note_that_cannot_be_written_is_logged(self):
        self.channel.note = mock.Mock(side_effect=OSError("disk full"))
        found = self.start(Recorder())
        with self.assertLogs("session", "WARNING") as logged:
            found.put(Logged(1, "g:1", None, Reply("a")))
            self.assertEqual(found.drain(WAIT), 0)
        self.assertIn("deliveries.jsonl: disk full", logged.output[0])

    def test_a_non_none_return_is_noted_once(self):
        handler = Recorder(returns="done")
        found = self.start(handler)
        found.put(Logged(1, "g:1", None, Reply("a")))
        self.assertEqual(found.drain(WAIT), 0)
        self.assertEqual(len(handler.calls), 1)
        (record,) = self.deliveries()
        self.assertEqual((record["state"], record["error"]), ("failed", "on_event returned str"))


class LoadTest(Case):
    """Each test loads modules of fresh names, so none is ever loaded twice."""

    def setUp(self):
        self.root = os.path.join(self.home, "handlers")
        os.mkdir(self.root)

    def module(self, body: str, *roots: str, name: str | None = None) -> str:
        name = name or "handler_" + uuid.uuid4().hex
        for root in roots or (self.root,):
            with open(os.path.join(root, name + ".py"), "w", encoding="utf-8") as f:
                f.write(body.replace("ROOT", os.path.basename(root)))
        return name

    def test_a_handler_module_loads_by_its_on_event(self):
        name = self.module("def on_event(call):\n    return None\n")
        (module,) = load_handlers((HookCallback(name, roots=(self.root,)),), (), load=True)
        assert isinstance(module, types.ModuleType)
        self.assertTrue(callable(module.on_event))

    def test_find_only_imports_nothing(self):
        name = self.module("raise SystemExit('imported')\n")
        self.assertEqual(
            load_handlers((HookCallback(name, roots=(self.root,)),), (), load=False), [None]
        )
        self.assertNotIn(name, sys.modules)

    def test_missing_module_or_on_event_is_a_config_error(self):
        no_entry = self.module("def on_hook(call):\n    return None\n")
        broken = self.module("raise ValueError('bad module')\n")
        for name, load, message in (
            ("handler_missing", False, "cannot find"),
            (no_entry, True, "missing on_event"),
            (broken, True, "bad module"),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(ConfigError, message):
                load_handlers((HookCallback(name, roots=(self.root,)),), (), load=load)

    def test_search_order_is_session_roots_then_handler_roots_then_sys_path(self):
        session_root = os.path.join(self.home, "session_root")
        other_root = os.path.join(self.home, "other_root")
        os.mkdir(session_root)
        os.mkdir(other_root)
        body = "WHERE = 'ROOT'\ndef on_event(call):\n    return None\n"
        first = self.module(body, session_root, self.root)
        (found,) = load_handlers(
            (HookCallback(first, roots=(self.root,)),), (session_root,), load=True
        )
        self.assertEqual(found.WHERE if found else None, "session_root")
        # a handler's module is found over every handler's roots, not only its own
        second = self.module(body, other_root)
        handlers = (
            HookCallback(second, roots=(self.root,)),
            HookCallback(first, roots=(other_root,)),
        )
        self.assertEqual(load_handlers(handlers, (), load=False), [None, None])
        with self.assertRaisesRegex(ConfigError, "cannot find"):
            load_handlers(handlers[:1], (), load=False)
