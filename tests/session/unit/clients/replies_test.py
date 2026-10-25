"""The tui clients' shared mid-turn reply machinery: state files, locks, reply lines, the watcher."""

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
        names = [old, old + ".lock", old + ".watch.lock", young, young + ".lock"]
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


class WatcherTest(SharedBase):
    def setUp(self):
        super().setUp()
        self.spawned = self.enterContext(mock.patch.object(replies.subprocess, "Popen"))

    def test_ensure_watcher_spawns_detached_unless_one_runs(self):
        replies.ensure_watcher(self.path, "some.watch")
        ((args, kwargs),) = self.spawned.call_args_list
        self.assertEqual(args[0][1:], ["-P", "-s", "-m", "some.watch", self.path])
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["cwd"], "/")
        with file_lock(self.path + ".watch", wait=False):
            replies.ensure_watcher(self.path, "some.watch")
        self.assertEqual(self.spawned.call_count, 1)

    def watch(self, settle, size=lambda: 0, clock=None):
        clock = clock or Clock()
        found = replies.watch(
            self.path, settle, size, clock=clock, sleep=clock.sleep, label="test reply"
        )
        return found, clock.now - 1000.0

    def test_idle_exit_after_nothing_pending(self):
        pending = [1, 1, 0]
        found, took = self.watch(lambda _deadline: pending.pop(0) if pending else 0)
        self.assertEqual(found, 0)
        self.assertLessEqual(took, 2 * replies.POLL + replies.IDLE_EXIT + replies.POLL)

    def test_quiet_and_lifetime_exits(self):
        _, took = self.watch(lambda _deadline: 1)
        self.assertGreaterEqual(took, replies.QUIET)
        self.assertLess(took, replies.QUIET + 2 * replies.POLL)
        sizes = iter(range(10**9))
        clock = Clock()

        def growing():
            clock.sleep(600.0)  # long polls keep this loop short
            return next(sizes)

        _, took = self.watch(lambda _deadline: 1, growing, clock)
        self.assertGreaterEqual(took, replies.LIFETIME)

    def test_settle_failure_counts_as_pending_and_is_said(self):
        calls = []

        def settle(deadline):
            calls.append(deadline)
            if len(calls) == 1:
                raise TimeoutError("run.json: locked")
            return 0

        self.watch(settle)
        self.assertIn("test reply: run.json: locked", self.stderr.getvalue())
        self.assertEqual(calls[0], 1000.0 + replies.WATCH_LOCK_WAIT)
        self.assertGreater(len(calls), 2)

    def test_second_watcher_exits_at_once(self):
        with file_lock(self.path + ".watch", wait=False):
            found, took = self.watch(lambda _deadline: 1)
        self.assertEqual((found, took), (0, 0.0))


class MainTest(SharedBase):
    def test_runs_with_one_absolute_state_file(self):
        self.assertEqual(replies.main([self.path], len), len(self.path))
        for argv in ([], ["relative.json"], [self.path, self.path]):
            with self.subTest(argv=argv):
                self.assertEqual(replies.main(argv, len), 2)
        self.assertIn("usage: watch <absolute state file>", self.stderr.getvalue())
