"""harnesses.end closes a test HOME's session harnesses and returns only once each has let go."""

import fcntl
import json
import os
import signal
import threading
import time
import uuid
from unittest import mock

from ensemble.session.harness.channel import Channel
from ensemble.session.harness.types import Op
from tests.session import harnesses
from tests.session.case import Case

SETTLE = 0.3  # seconds a fake harness keeps writing after its close reply


class FakeHarness(threading.Thread):
    """Holds a harness dir under home; on CLOSE replies, keeps writing SETTLE, then lets go."""

    def __init__(self, home: str, *, answers: bool = True):
        super().__init__(daemon=True)
        directory = os.path.join(home, ".ensemble", "harness", uuid.uuid4().hex)
        os.makedirs(directory, 0o700)
        self.channel, self.answers, self.ops = Channel(directory), answers, []
        self.halt = threading.Event()
        self.channel.hold()

    def run(self):
        while not self.halt.is_set():
            for ident, op in self.channel.requests():
                self.ops.append(op)
                if self.answers:
                    self.channel.reply(ident, ok=True)
                    time.sleep(SETTLE)
                    self.channel.write_status({"state": "exited"})
                    self.channel.release()
                    return
            time.sleep(0.02)
        self.channel.release()


class EndTest(Case):
    def setUp(self):
        super().setUp()
        self.ending: dict[int, tuple[FakeHarness, tuple[signal.Signals, ...]]] = {}
        self.signals: list[tuple[int, signal.Signals]] = []

    def harness(self, **kwargs) -> FakeHarness:
        harness = FakeHarness(self.home, **kwargs)
        harness.start()
        self.addCleanup(harness.join, 5)
        self.addCleanup(harness.halt.set)
        return harness

    def test_a_home_without_harnesses_ends_at_once(self):
        harnesses.end(self.home)

    def test_a_home_whose_ensemble_is_a_file_ends_at_once(self):
        os.rmdir(self.ensemble)
        open(self.ensemble, "w").close()
        harnesses.end(self.home)

    def test_a_live_harness_is_closed_and_awaited_past_its_reply(self):
        harness = self.harness()
        harnesses.end(self.home)
        self.assertEqual(harness.ops, [Op.CLOSE])
        self.assertFalse(harness.channel.alive())
        self.assertEqual(harness.channel.status(), {"state": "exited"})

    def test_a_harness_already_gone_is_not_asked(self):
        harness = self.harness()
        harness.halt.set()
        harness.join(5)
        harnesses.end(self.home)
        self.assertEqual(harness.ops, [])

    def test_a_harness_still_running_past_the_wait_fails_naming_its_dir(self):
        harness = self.harness(answers=False)
        with self.assertRaises(AssertionError) as caught:
            harnesses.end(self.home, wait=0.5)
        self.assertIn(harness.channel.directory, str(caught.exception))
        self.assertTrue(harness.channel.alive())

    def stuck(self, pid, *, ends_on=(signal.SIGTERM,)) -> FakeHarness:
        """A harness that answers nothing, naming `pid`; patched os.kill ends it on `ends_on`."""
        harness = self.harness(answers=False)
        with open(harness.channel.status_file, "w", encoding="utf-8") as f:
            json.dump({"state": "running", "pid": pid}, f)
        self.ending[pid] = (harness, ends_on)
        return harness

    def kill(self, pid, sig):
        self.signals.append((pid, sig))
        if pid not in self.ending:
            raise OverflowError("signed integer is greater than maximum")
        harness, ends_on = self.ending[pid]
        if sig in ends_on:
            harness.halt.set()

    def end(self, **kwargs) -> AssertionError:
        with (
            mock.patch("os.kill", side_effect=self.kill),
            self.assertRaises(AssertionError) as caught,
        ):
            harnesses.end(self.home, **kwargs)
        return caught.exception

    def test_a_stuck_harness_is_terminated_and_still_fails(self):
        harness = self.stuck(4242)
        failed = self.end(wait=0.3)
        self.assertIn(harness.channel.directory, str(failed))
        self.assertEqual(self.signals, [(4242, signal.SIGTERM)])
        self.assertFalse(harness.channel.alive())

    def test_a_harness_ignoring_sigterm_is_killed_and_still_fails(self):
        harness = self.stuck(4242, ends_on=(signal.SIGKILL,))
        with mock.patch.object(harnesses, "KILL_WAIT", 0.3):
            failed = self.end(wait=0.3)
        self.assertIn(harness.channel.directory, str(failed))
        self.assertEqual(self.signals, [(4242, signal.SIGTERM), (4242, signal.SIGKILL)])
        self.assertFalse(harness.channel.alive())

    def test_an_unsignallable_pid_is_named_and_never_signalled(self):
        for pid in (True, 0, -1, 1, os.getpid(), None, "7"):
            with self.subTest(pid=pid):
                harness = self.harness(answers=False)
                with open(harness.channel.status_file, "w", encoding="utf-8") as f:
                    json.dump({"state": "running", "pid": pid}, f)
                with (
                    mock.patch("os.kill") as kill,
                    self.assertRaises(AssertionError) as caught,
                ):
                    harnesses.end(self.home, wait=0.2)
                self.assertIn(harness.channel.directory, str(caught.exception))
                kill.assert_not_called()
                harness.halt.set()
                harness.join(5)

    def test_a_pid_os_kill_refuses_is_named_and_the_rest_still_signalled(self):
        refused = self.harness(answers=False)
        with open(refused.channel.status_file, "w", encoding="utf-8") as f:
            json.dump({"state": "running", "pid": 2**40}, f)
        harness = self.stuck(4242)
        failed = self.end(wait=0.3)
        self.assertIn(refused.channel.directory, str(failed))
        self.assertIn(harness.channel.directory, str(failed))
        self.assertIn((4242, signal.SIGTERM), self.signals)
        self.assertFalse(harness.channel.alive())

    def test_no_lock_wait_outlives_end(self):
        held = threading.Event()
        self.addCleanup(held.set)
        flock = fcntl.flock

        def holding(fd, operation):
            flock(fd, operation)
            if not operation & fcntl.LOCK_NB:
                held.wait(5)

        harness = self.stuck(4242)
        with (
            mock.patch.object(harnesses.fcntl, "flock", side_effect=holding),
            mock.patch.object(harnesses, "KILL_WAIT", 0.3),
        ):
            self.end(wait=0.3)
        self.assertFalse(harness.channel.alive())

    def test_a_harness_that_let_go_is_not_named_with_no_time_left(self):
        harness = self.harness()
        harness.halt.set()
        harness.join(5)
        self.assertTrue(harnesses._let_go(harness.channel, 0.0))


class LetGoTest(Case):
    def held(self) -> Channel:
        directory = os.path.join(self.ensemble, "harness", uuid.uuid4().hex)
        os.makedirs(directory, 0o700)
        channel = Channel(directory)
        channel.hold()
        self.addCleanup(channel.release)
        return channel

    def test_a_wait_that_times_out_leaves_no_thread_behind(self):
        channel = self.held()
        before = set(threading.enumerate())
        self.assertFalse(harnesses._let_go(channel, 0.0))
        self.assertEqual({t for t in threading.enumerate() if t.is_alive()} - before, set())
        channel.release()
        self.assertTrue(harnesses._let_go(channel, 1))
        self.assertFalse(channel.alive())

    def test_a_holder_releasing_mid_wait_is_let_go(self):
        channel = self.held()
        asked = threading.Event()
        holder = threading.Thread(target=lambda: (asked.wait(), channel.release()))
        holder.start()
        self.addCleanup(holder.join)
        with mock.patch.object(
            harnesses.fcntl, "flock", side_effect=_after_first(fcntl.flock, asked.set)
        ):
            self.assertTrue(harnesses._let_go(channel, harnesses.WAIT))
        self.assertFalse(channel.alive())


def _after_first(flock, then):
    calls = []

    def call(fd, operation):
        calls.append(operation)
        try:
            return flock(fd, operation)
        finally:
            if len(calls) == 1:
                then()

    return call
