"""A run planned and executed in process over
session/tests/integration/clients/claude/fake_claude.py (on PATH as `claude`, the session config's
Client table), HOME and CLAUDE_CONFIG_DIR temp: the callback order over a real process (FR-17), the
fix resume (FR-21), a usage limit (FR-58).
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

from ensemble.delegate import Progress, Spec
from ensemble.delegate.composer import Composer
from ensemble.delegate.engine.drivers import AgentDriver
from ensemble.delegate.engine.runner import FIX
from ensemble.delegate.engine.types import Begun, Finished, Layout, Limit

from delegate.tests.case import Case, packaged
from delegate.tests.integration.cli import DONE, RESETS, SID, UNSAVED, UNSAVED_ERROR
from delegate.tests.replies import block, mark, option, prompt, running
from delegate.tests.runner import executed
from session.tests.integration.clients.claude import fake_claude


class Base(Case):
    """dummy-tester's echo over the fake `claude` in workdir `work`, the cwd its agent dir."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.realpath(tmp.name)
        self.work, bin_dir = os.path.join(root, "work"), os.path.join(root, "bin")
        os.mkdir(bin_dir)
        self.out = os.path.join(self.work, "out.md")
        self.scenario, self.log = (
            os.path.join(root, "scenario.json"),
            os.path.join(root, "log.jsonl"),
        )
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {
                    "PATH": fake_claude.install(bin_dir),
                    "CLAUDE_CONFIG_DIR": os.path.join(root, "config"),
                    fake_claude.ENV: self.scenario,
                },
            )
        )
        self.driver = AgentDriver(packaged())

    def go(self, steps, **scenario):
        """executed() with the fake's `steps`; self.events gets each callback."""
        with open(self.scenario, "w") as f:
            json.dump({"steps": steps, **scenario, "log": self.log}, f)
        self.events = []
        with redirect_stderr(io.StringIO()):
            return executed(
                Composer().resolve("dummy-tester"),
                Spec(
                    "Hello.",
                    self.work,
                    task="echo",
                    client="claude",
                    sid=SID,
                    out=self.out,
                ),
                on_event=self.events.append,
                driver=self.driver,
            )

    def calls(self) -> list[dict]:
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    def kinds(self) -> list[str]:
        with open(os.path.join(Layout(self.work).run, "run.jsonl")) as f:
            return [json.loads(line)["kind"] for line in f]


class Callbacks(Base):
    def test_begun_then_a_progress_per_mark_then_finished_over_a_real_process(self):
        result = self.go(
            [mark("start", "echoing"), "Echoing.", mark("draft", "half  way"), block(DONE)]
        )
        self.assertEqual((result.returncode, result.outcome.status, result.error), (0, "done", ""))
        self.assertEqual(
            self.events,
            [
                Begun(SID),
                Progress("start", "echoing"),
                Progress("draft", "half way"),
                Finished(result),
            ],
        )
        with open(self.out) as f:
            self.assertEqual(f.read(), DONE["deliverable"])


class Fix(Base):
    def test_an_invalid_outcome_is_fixed_by_one_resume_of_the_session(self):
        result = self.go([mark("start", "echoing"), block(UNSAVED)], resume=[block(DONE)])
        self.assertEqual((result.returncode, result.outcome.status), (0, "done"))
        self.assertEqual(self.events, [Begun(SID), Progress("start", "echoing"), Finished(result)])
        first, second = self.calls()
        self.assertEqual((option(first, "--session-id"), option(second, "--resume")), (SID, SID))
        self.assertEqual(prompt(second), FIX.format(error=UNSAVED_ERROR))
        self.assertEqual(
            self.kinds(),
            [
                "session",
                "input",
                "client_session",
                "progress",
                "invalid",
                "session",
                "client_session",
                "result",
                "end",
            ],
        )


class UsageLimit(Base):
    def test_a_limit_stops_the_session_and_ends_the_run(self):
        result = self.go(
            ["Working.", {"kind": "limit", "resets_at": RESETS}, block(DONE)], hang=True
        )
        self.assertEqual((result.outcome, result.limit), (None, Limit(RESETS)))
        self.assertTrue(result.error.startswith("usage limit; resets at "), result.error)
        self.assertEqual(self.events, [Begun(SID), Finished(result)])
        (call,) = self.calls()
        self.assertFalse(running(call["pid"]))
        self.assertEqual(
            self.kinds(), ["session", "input", "client_session", "limit", "result", "end"]
        )
        self.assertFalse(os.path.exists(self.out))


if __name__ == "__main__":
    unittest.main()
