"""`ensemble-crew hide --all` and `show --all` over a worker, a role run and a pipeline on the real
commands (tests/manager/live_pipeline.py): hide closes each one's panes in the manager's window and
marks it hidden; show opens back one agent pane each, no harness, and clears hidden.
"""

import json

from tests.delegate.replies import mark
from tests.manager.live_crew import MANAGER, ROLE
from tests.manager.live_pipeline import ID, ROUTER, LivePipeline

START = mark("start", "working")


class ViewTest(LivePipeline):
    def shown(self) -> set[str]:
        """The sessions whose @pane is a pane of the manager's window."""
        window = self.server.tmux("display-message", "-p", "-t", f"={MANAGER}:", "#{window_id}")
        panes = self.server.panes(window.stdout.strip())
        listed = self.server.tmux("list-sessions", "-F", "#{session_name}\t#{@pane}").stdout
        rows = (line.split("\t", 1) for line in listed.splitlines())
        return {name for name, pane in rows if pane in panes and name != MANAGER}

    def hidden(self) -> dict[str, bool]:
        lines = self.ok(self.crew("status")).stdout.splitlines()
        return {status["name"]: status["hidden"] for status in map(json.loads, lines)}

    def test_hide_all_then_show_all_over_three_kinds(self):
        self.ok(self.worker("w"))
        role = self.record(self.role("r", steps=[START]))
        pipeline = self.record(self.crew("run", "p", "--", *ROUTER, **self.scene("p", [START])))
        tuis = {"w", f"{ROLE}-{role.sid[:8]}", f"engineer-{ID}-{pipeline.sid[:8]}"}
        self.until(lambda: self.shown() == tuis, f"{tuis} shown")

        self.ok(self.crew("hide", "--all"))
        self.until(lambda: not self.shown(), "no pane shown")
        self.assertEqual(self.hidden(), dict.fromkeys(("p", "r", "w"), True))

        self.ok(self.crew("show", "--all"))
        self.until(lambda: self.shown() == tuis, f"{tuis} shown again")
        self.assertEqual(self.hidden(), dict.fromkeys(("p", "r", "w"), False))
