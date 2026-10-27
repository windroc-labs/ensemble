"""harnesses.end closes a test HOME's session harnesses and returns only once each has let go."""

import os
import threading
import time
import uuid

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
