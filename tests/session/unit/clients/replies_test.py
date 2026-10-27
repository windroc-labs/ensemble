"""The tui clients' shared mid-turn reply machinery: state files, locks, reply lines, the pump's
settle."""

import contextlib
import io
import json
import os
import tempfile
import time
from unittest import mock

from ensemble.session.clients import replies
from ensemble.utils import STALE_AGE, file_lock, read_reply
from tests.session.case import Case


class Clock:
    """A fake monotonic clock that sleep advances."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class SharedBase(Case):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.path = os.path.join(self.root, "run.json")
        self.events = os.path.join(self.root, "events")
        open(self.events, "w", encoding="utf-8").close()
        self.stderr = self.enterContext(contextlib.redirect_stderr(io.StringIO()))


class StateTest(SharedBase):
    def test_load_gives_fresh_for_missing_malformed_or_invalid(self):
        def fresh():
            return {"fresh": True}

        def valid(state):
            return isinstance(state, dict) and state.get("ok") is True

        self.assertEqual(replies.load(self.path, fresh, valid), {"fresh": True})
        for data in ("{not json", json.dumps({"ok": False}), json.dumps([1])):
            with open(self.path, "w", encoding="utf-8") as stream:
                stream.write(data)
            self.assertEqual(replies.load(self.path, fresh, valid), {"fresh": True})
        with open(self.path, "w", encoding="utf-8") as stream:
            json.dump({"ok": True}, stream)
        self.assertEqual(replies.load(self.path, fresh, valid), {"ok": True})

    def test_load_refuses_an_oversized_file(self):
        with open(self.path, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"ok": True, "pad": "x" * replies.STATE_MAX}))
        self.assertEqual(replies.load(self.path, dict, lambda state: isinstance(state, dict)), {})

    def test_directory_is_private_and_kept(self):
        folder = os.path.join(self.root, "replies")
        replies.directory(folder)
        replies.directory(folder)
        self.assertEqual(os.stat(folder).st_mode & 0o777, 0o700)

    def test_prune_drops_old_state_with_its_locks_only(self):
        old, young = os.path.join(self.root, "old.json"), os.path.join(self.root, "young.json")
        names = [old, old + ".lock", young, young + ".lock"]
        for name in names:
            open(name, "w", encoding="utf-8").close()
        then = time.time() - STALE_AGE - 10
        os.utime(old, (then, then))
        replies.prune(self.root)
        self.assertEqual(sorted(os.listdir(self.root)), ["events", "young.json", "young.json.lock"])

    def test_locked_waits_until_its_deadline(self):
        clock = Clock()
        with (
            file_lock(self.path, wait=False),
            self.assertRaises(TimeoutError),
            replies.locked(self.path, clock() + 1, clock, clock.sleep),
        ):
            pass
        self.assertGreaterEqual(clock.now - 1000.0, 1)
        with replies.locked(self.path, clock(), clock, clock.sleep):
            pass


class EmitTest(SharedBase):
    def lines(self):
        with open(self.events, encoding="utf-8") as stream:
            return [line.split()[1:] for line in stream if line.strip()]

    def test_one_line_and_stored_text_per_key(self):
        replies.emit(self.events, "w1", "native:12", "the answer")
        replies.emit(self.events, "w1", "native:12", "another")
        replies.emit(self.events, "w1", "native:40", "second")
        lines = self.lines()
        self.assertEqual([words[:2] for words in lines], [["w1", "reply"], ["w1", "reply"]])
        self.assertEqual(read_reply(self.events, lines[0][2]), "the answer")
        self.assertEqual(read_reply(self.events, lines[1][2]), "second")


class PumpedTest(SharedBase):
    def test_settles_an_existing_state_file_at_once(self):
        calls = []
        replies.pumped(self.path, calls.append, "test reply")
        self.assertEqual(calls, [])
        open(self.path, "w", encoding="utf-8").close()
        with mock.patch.object(replies.time, "monotonic", return_value=1000.0):
            replies.pumped(self.path, calls.append, "test reply")
        self.assertEqual(calls, [1000.0])

    def test_a_held_lock_waits_for_the_next_pump_and_an_oserror_is_said(self):
        open(self.path, "w", encoding="utf-8").close()
        for error in (TimeoutError("run.json: locked"), OSError("disk")):

            def settle(_deadline, error=error):
                raise error

            replies.pumped(self.path, settle, "test reply")
        self.assertEqual(self.stderr.getvalue(), "test reply: disk\n")
