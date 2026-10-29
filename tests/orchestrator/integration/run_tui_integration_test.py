"""A tui LinearRun (entry.py tui) through the real harness on a private tmux server (live_tmux)
whose manager session a client shows, fake_claude interactive as client `claude`: its opaque
display (FR-48) puts the TUI `<role>-<ID>-<sid8>` beside the display's split_from session; the run
writes back through `writeback` and leaves its TUI open.
"""

import os
import unittest

from ensemble.orchestrator.engine import runs
from tests.delegate.replies import block, mark
from tests.orchestrator.integration.flow_fixtures import (
    ENTRY,
    MANAGER,
    PROJECT,
    TmuxFlow,
    steps,
)
from tests.orchestrator.unit.board_ids import role_id
from tests.orchestrator.unit.run_fixtures import ID, SID, UUID
from tests.orchestrator.unit.writeback_fixtures import PRD

TUI = f"pm-{ID}-{SID[:8]}"
LEDGER = ["running:1", "start", "retitle", "comment", f"attach:{PRD}", "move:in_review", "outcome"]
DONE = {
    "status": "done",
    "title": "Session Registry",
    "summary": "PRD for the registry.",
    "url": PRD,
}


class Tui(TmuxFlow):
    def setUp(self):
        super().setUp()
        self.server.attach(MANAGER)
        self.manager = self.server.inside(MANAGER)["TMUX_PANE"]
        self.fake.issue(
            "Session registry",
            identifier=ID,
            id=UUID,
            state="in_progress",
            delegate=role_id("pm"),
            project=PROJECT,
        )
        self.scene([f"{mark('start', 'a PRD.')}\n{block(DONE)}"])

    def launch(self, display):
        """The claimed run's LinearRun detached in tui mode at `display`, until written back."""
        runs.note(
            os.path.join(self.logs, "runs.jsonl"),
            "start",
            issue=ID,
            sid=SID,
            role="pm",
            client="claude",
            delegate_kind="harness",
            uuid=UUID,
            project=PROJECT["id"],
            target=None,
            task=None,
        )
        res = self.script(
            ENTRY,
            "tui",
            ID,
            SID,
            "pm",
            "claude",
            display,
            "Do it.\n",
            **self.server.inside(MANAGER),
        )
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)
        self.wait_end(SID, ID, role="pm")
        self.assertTrue(self.live(TUI))
        self.assertEqual((self.state(ID), self.ledger(ID)), ("in_review", {SID: LEDGER}))
        self.assertEqual(self.said(), steps(*LEDGER))

    def test_the_tui_shows_beside_the_displays_split_from(self):
        self.launch(f"{MANAGER}//{MANAGER}")
        panes, pane = self.server.panes(f"={MANAGER}:"), self.server.pane_of(TUI)
        self.assertEqual(set(panes), {self.manager, pane})
        self.assertGreater(panes[pane][0], panes[self.manager][0])


if __name__ == "__main__":
    unittest.main()
