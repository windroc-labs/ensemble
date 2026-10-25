"""A ready issue on a local board runs to review (spec AC1, AC2): bin/ensemble-orchestrator router
--issue as a real process with tracker = "local", the run's harness calling this installation's
`writeback` back on each event; replayed events and a lost ledger post nothing twice.
"""

import hashlib
import json
import os
import re
import subprocess
import unittest

from ensemble.orchestrator.engine.config import BOARD_ENV
from ensemble.orchestrator.trackers.local.local_tracker import LocalTracker
from orchestrator_tests.integration.flow_fixtures import BIN, TIMEOUT, BinFlow, usage

from delegate.tests.replies import block, mark

ID = "LOC-1"
NAME = f"{ID} Queue survey.md"
STARTED, READING = "Comparing three queues.", "Read the queue docs."
REPORT = "https://github.com/windroc/team-docs/blob/main/Research/queues.md"
OUTCOME = {
    "status": "done",
    "title": "Queue survey",
    "summary": "Three queues compared.",
    "url": REPORT,
}
ISSUE = f"""---
id: {ID}
title: "Queue survey"
created: 2026-10-20T09:00:00+00:00
---
### Description
Survey the three queues.
"""
HEADER = re.compile(r"#### (self|other) · \S+ \S+(?: <!-- id=(\S+) key=(\S+) -->)?")
HISTORY = re.compile(r"- \S+ \S+ (\w+) (self|other)")


class RunToReview(BinFlow):
    def setUp(self):
        super().setUp()
        self.board = os.path.join(self.root, "board")
        state = os.path.join(self.board, ".state")
        self.workdirs, self.logs = os.path.join(state, "work"), os.path.join(state, "logs")
        config = os.path.join(self.ensemble, "config.local.toml")
        with open(config, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text.count('tracker = "linear"\n'), 1)
        with open(config, "w", encoding="utf-8") as f:
            f.write(text.replace('tracker = "linear"\n', 'tracker = "local"\n'))
        ready = os.path.join(self.board, "researcher", "ready")
        os.makedirs(ready)
        with open(os.path.join(ready, NAME), "w", encoding="utf-8") as f:
            f.write(ISSUE)

    def bin_env(self):
        return {**super().bin_env(), BOARD_ENV: self.board}

    def issue_file(self, state):
        return os.path.join(self.board, "researcher", state, NAME)

    def text(self):
        with open(self.issue_file("review"), encoding="utf-8") as f:
            return f.read()

    def comments(self, ident=ID):
        """(origin, key, body) of each comment, in file order."""
        detail = LocalTracker(self.board, ("researcher",), prefix="LOC").detail(ident)
        keys = [m[3] for m in map(HEADER.fullmatch, self.text().splitlines()) if m]
        return [(c.origin, key, c.body) for c, key in zip(detail.comments, keys, strict=True)]

    def history(self):
        return [m.groups() for m in map(HISTORY.fullmatch, self.text().splitlines()) if m]

    def run_to_review(self):
        self.scene([mark("start", STARTED), mark("sources", READING), block(OUTCOME)])
        res = self.router("--issue", ID)
        self.assertEqual((res.returncode, res.stdout), (0, ""), (res.stderr, self.said()))
        [row] = self.runs()
        self.assertEqual((row["uuid"], row["role"]), (ID, "researcher"))
        self.wait_end(row["sid"], ID, role="researcher")
        return row["sid"]

    def writeback(self, event):
        res = subprocess.run(
            [BIN, "writeback", "--issue", ID, "--workdir", os.path.join(self.workdirs, ID)],
            cwd=self.root,
            env=self.bin_env(),
            input=json.dumps({"v": 1, "attempt": 1, **event}) + "\n",
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)

    def test_a_ready_issue_runs_to_review(self):
        sid = self.run_to_review()
        self.assertFalse(os.path.exists(self.issue_file("ready")))
        self.assertFalse(os.path.exists(self.issue_file("running")), self.said())
        self.assertEqual(
            self.history(), [("ready", "other"), ("running", "self"), ("review", "self")]
        )
        (session, *rest) = self.comments()
        self.assertEqual(session[:2], ("self", f"{sid}:session"))
        self.assertRegex(session[2], rf"^Run {sid} · done · .* · exit 0\n")
        self.assertEqual(
            rest,
            [
                ("self", f"{sid}:start", f"Research started: {STARTED}"),
                (
                    "self",
                    f"{sid}:progress:sources:{_digest(READING)}",
                    f"Progress (sources): {READING}",
                ),
                ("self", f"{sid}:comment", f"Three queues compared.\n\n{REPORT}"),
            ],
        )

    def test_replays_post_nothing_twice(self):
        sid = self.run_to_review()
        comments, history = self.comments(), self.history()
        events = [
            {"kind": "begun", "sid": sid},
            {"kind": "progress", "sid": sid, "name": "start", "text": STARTED},
            {"kind": "progress", "sid": sid, "name": "sources", "text": READING},
        ]
        finished = {
            "kind": "finished",
            "sid": sid,
            "returncode": 0,
            "outcome": {**OUTCOME, "questions": [], "files": [], "deliverable": ""},
            "error": "",
            "limit": None,
        }
        for event in [*events, finished]:
            self.writeback(event)
        self.assertEqual((self.comments(), self.history()), (comments, history))
        os.unlink(os.path.join(self.workdirs, ID, "writeback.json"))
        self.writeback(finished)
        self.scene([], usage=usage(0.5))
        res = self.router()
        self.assertEqual(res.returncode, 0, (res.stderr, self.said()))
        again = self.comments()
        self.assertEqual([c[:2] for c in again], [c[:2] for c in comments])
        self.assertEqual(again[1:], comments[1:])  # the session comment, edited in place at most
        self.assertEqual(self.history(), history)


def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()[:12]


if __name__ == "__main__":
    unittest.main()
