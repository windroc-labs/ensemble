"""FR-25 and FR-29 end to end: bin/ensemble-orchestrator router by absolute path, a launchd-like PATH and
no PYTHONPATH. The router claims, writes the start row and exits while the run's harness and agent
go on; the harness finds `ensemble-orchestrator` in this installation's bin/ and calls it back on each
event, so the issue gets its session comment, progress and outcome once each.
"""

import os
import unittest

from tests.delegate.replies import block, mark
from tests.orchestrator.integration.flow_fixtures import (
    PROJECT,
    TIMEOUT,
    UPDRAFT,
    BinFlow,
)
from tests.orchestrator.integration.segments import agent_calls
from tests.orchestrator.unit.board_ids import HARNESS_USER, role_id
from tests.session import live_tmux

ID = "TASK-7"
STARTED, READY = "Building the session registry.", "Added the session registry."
PR = "https://github.com/acme/ensemble/pull/1"
DONE = block(
    {
        "status": "done",
        "title": "Session registry",
        "summary": READY,
        "url": PR,
        "updraft": UPDRAFT,
    }
)


class MinimalPath(BinFlow):
    def setUp(self):
        super().setUp()
        self.fake.issue(
            "ENG: Session registry",
            identifier=ID,
            delegate=role_id("engineer"),
            project=PROJECT,
            description="Add a session registry.",
        )

    def test_the_router_exits_and_the_harness_calls_this_installations_writeback(self):
        gate = os.path.join(self.root, "gate")
        self.scene([mark("start", STARTED), DONE], gate=gate)
        res = self.router("--issue", ID)
        self.assertEqual((res.returncode, res.stdout), (0, ""), (res.stderr, self.said()))
        [row] = self.runs()
        sid = row["sid"]
        harness = self.harness("engineer", ID, sid)
        live_tmux.wait(lambda: agent_calls(self.calls()), TIMEOUT, "the agent at its gate")
        self.assertTrue(self.live(harness))
        with open(gate, "w"):
            pass
        self.wait_end(sid, ID)
        self.assertEqual(self.state(ID), "in_review")
        (author, session), *rest = self.comments(ID)
        self.assertEqual(author, HARNESS_USER)
        self.assertRegex(session, rf"^Run {sid} · done · .* · exit 0\n")
        engineer = role_id("engineer")
        self.assertEqual(
            rest,
            [
                (engineer, f"Build started: {STARTED}"),
                (engineer, f"Build ready:\nAgent report before delivery:\n\n> {READY}"),
            ],
        )
        self.assertEqual(
            self.ledger(ID),
            {sid: ["running:1", "start", "comment", f"attach:{PR}", "move:in_review", "outcome"]},
        )
        self.assertEqual(len(self.sent("linear.M_COMMENT_ID", HARNESS_USER)), 1)


if __name__ == "__main__":
    unittest.main()
