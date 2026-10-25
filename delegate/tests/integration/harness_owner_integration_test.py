"""Foreground ownership survives fresh views and excludes replacement or recovery."""

import os
import subprocess
import sys
import time

from ensemble.delegate import ConfigError
from ensemble.delegate.engine.harness_delegate import HarnessDelegate

from delegate.tests.case import REPO, child_env
from delegate.tests.fakes.harness_session import HarnessSession, RecordingHost
from delegate.tests.harness import SID, HarnessCase

RUN = """import os, sys
from ensemble.delegate import HarnessDelegate, Spec
from ensemble.delegate.engine.types import Begun
from delegate.tests.fakes.harness_session import HarnessSession
from delegate.tests.replies import block
from delegate.tests.unit.runs import DONE
from ensemble.session import Exited, Reply
HarnessSession.reset()
HarnessSession.script = (Reply(block(DONE)), Exited(0))
owner = HarnessDelegate("dummy-tester", Spec("input", sys.argv[1], task="echo", sid=sys.argv[2], client="fake"), session_cls=HarnessSession)
def observe(event):
    if isinstance(event, Begun):
        with open(os.path.join(sys.argv[1], "running"), "w") as marker:
            marker.write("started")
        sys.stdin.readline()
result = owner.run(observe)
sys.exit(result.returncode)
"""


class ForegroundOwner(HarnessCase):
    def start_owner(self):
        child = subprocess.Popen(
            [sys.executable, "-P", "-c", RUN, self.work, SID],
            cwd=REPO,
            env=child_env(None, REPO),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self.cleanup_owner, child)
        deadline = time.monotonic() + 30
        while not os.path.exists(os.path.join(self.work, "running")):
            if child.poll() is not None or time.monotonic() > deadline:
                child.kill()
                output, error = child.communicate(timeout=10)
                self.fail(f"foreground owner did not start: {output} {error}")
            time.sleep(0.01)
        return child

    def cleanup_owner(self, child):
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)

    def finish_owner(self, child):
        output, error = child.communicate("answer\n", timeout=30)
        self.assertEqual(child.returncode, 0, f"{output} {error}")
        self.assertTrue(self.metadata()["completed"])
        self.assertEqual(self.metadata()["result"]["outcome"]["status"], "done")

    def test_fresh_headless_view_sees_foreground_owner_and_resume_starts_nothing(self):
        child = self.start_owner()
        before = self.metadata()
        view = HarnessDelegate.attach(self.work, session_cls=HarnessSession)
        self.assertTrue(view.alive())
        self.assertTrue(view.status().alive)
        record = view.resume()
        self.assertEqual(record.sid, SID)
        self.assertEqual(self.metadata(), before)
        self.assertEqual(RecordingHost.launches, [])
        self.finish_owner(child)
        self.assertFalse(view.alive())

    def test_constructor_cannot_replace_a_foreground_run_or_lose_its_finished_result(self):
        child = self.start_owner()
        before = self.metadata()
        with self.assertRaisesRegex(ConfigError, "still alive"):
            self.make()
        self.assertEqual(self.metadata(), before)
        self.finish_owner(child)
        self.make()
        self.assertEqual(self.metadata()["attempt"], 2)

    def test_crashed_foreground_owner_releases_ownership(self):
        child = self.start_owner()
        child.kill()
        child.communicate(timeout=10)
        self.assertFalse(HarnessDelegate.attach(self.work, session_cls=HarnessSession).alive())
        self.make()
        self.assertEqual(self.metadata()["attempt"], 2)
