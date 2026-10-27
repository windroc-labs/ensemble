"""attach over the real ensemble-session ls --here --records (tests/manager/live_crew.py) adopts
only a Record its roster launched: a crew run killed after its starter printed the Record but
before the commit; never an orphan started by the layer commands themselves, nor a Record an agent
forged on its own session through its inherited $TMUX.
"""

import json
import os
import shlex
import uuid

from tests.delegate.replies import mark
from tests.manager.live_crew import LiveCrew

START = mark("start", "echoing")


class OrphanTest(LiveCrew):
    def test_attach_adopts_no_orphan_whatever_its_record_names(self):
        self.orphan("o")
        argv = self.role_argv("r")[1:]
        events = os.path.join(self.root, "orphan-events")
        self.record(
            self.call("ensemble-delegate", *argv, "--events", events, **self.scene("r", [START]))
        )
        for _ in range(2):
            self.ok(self.crew("attach"))
            self.assertEqual(self.roster(), {})
        self.assertTrue(self.live("o"))

    def test_a_record_an_agent_forged_is_neither_adopted_nor_run(self):
        agent = self.orphan("agent")
        pwned = os.path.join(self.root, "pwned")
        shell = ["sh", "-c", f": > {shlex.quote(pwned)}"]
        sid = str(uuid.uuid4())
        for forged in (
            {"name": "forged", "sid": sid, "ctl": shell, "resume": shell},
            {"name": "forged", "sid": sid, "ctl": shell[:1], "resume": [shell[0], "resume", "x"]},
            {"name": "forged", "sid": sid},
        ):
            with self.subTest(forged=forged):
                line = json.dumps({**json.loads(agent.to_line()), **forged})
                self.ok(self.call("tmux", "set-option", "@record", line, inside="agent"))
                self.ok(self.crew("attach"))
                self.assertEqual(self.roster(), {})
                self.ok(self.crew("status"))
                self.assertFalse(os.path.exists(pwned))
        line = json.dumps({**json.loads(agent.to_line()), "ctl": shell, "resume": shell})
        self.ok(self.call("tmux", "set-option", "@record", line, inside="agent"))
        self.ok(self.crew("close-all"))
        self.assertFalse(os.path.exists(pwned))


class CrashTest(LiveCrew):
    def test_a_worker_whose_crew_died_before_the_commit_is_adopted_under_its_alias(self):
        record = self.crashed("w", self.worker_argv("w1"), **self.scene("w"))
        self.assertEqual((record.kind, record.name), ("worker", "w1"))

    def test_a_role_run_whose_crew_died_before_the_commit_is_adopted_under_its_alias(self):
        record = self.crashed("r", self.role_argv("r"), **self.scene("r", [START]))
        self.assertEqual((record.kind, len(record.sessions)), ("role", 1))
