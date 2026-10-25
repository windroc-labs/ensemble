"""`ensemble-orchestrator writeback` as a subprocess, one DelegateEvent line on stdin per call, FakeLinear
over HTTP: the run's claim comes from its runs.jsonl start row, each step is written once however
often its event arrives.
"""

import json
import os
import re
import subprocess
import unittest

from ensemble.orchestrator.engine import runs
from orchestrator_tests.case import PY
from orchestrator_tests.integration.flow_fixtures import (
    ACCOUNTS,
    HARNESS_EMAIL,
    KEYS,
    PROJECT,
    TIMEOUT,
    Flow,
)
from orchestrator_tests.unit.board_ids import comment_id
from orchestrator_tests.unit.run_fixtures import ID, SESSION, SID, UUID
from orchestrator_tests.unit.writeback_fixtures import DOC

ENTRY = os.path.join(os.path.dirname(__file__), "entry.py")

PICK = "picked the task."


class Writeback(Flow):
    def setUp(self):
        super().setUp()
        self.rd = os.path.join(self.workdirs, ID)
        os.makedirs(self.rd)
        self.fake.issue(
            "Session registry",
            identifier=ID,
            id=UUID,
            state="in_progress",
            assignee=self.ids[ACCOUNTS["researcher"]],
            project=PROJECT,
        )

    def claim(self, role="researcher", key=None):
        runs.note(
            os.path.join(self.logs, "runs.jsonl"),
            "start",
            issue=ID,
            sid=SID,
            role=role,
            client="claude",
            delegate_kind="harness",
            uuid=UUID,
            project=PROJECT["id"],
            target=None,
            key=key or KEYS[role],
        )

    def feed(self, kind, attempt=1, **fields):
        line = json.dumps({"v": 1, "kind": kind, "sid": SID, "attempt": attempt, **fields})
        return subprocess.run(
            [*PY, ENTRY, "writeback", "--issue", ID, "--workdir", self.rd],
            cwd=self.root,
            env=self.env(),
            input=line + "\n",
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )

    def ok(self, kind, attempt=1, **fields):
        res = self.feed(kind, attempt, **fields)
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)

    def stored(self):
        return [(self.email(c["user"]), c["id"]) for c in self.issue(ID)["comments"]]


class BegunAndProgress(Writeback):
    def test_the_session_comment_as_the_harness_and_marks_as_the_role_each_once(self):
        self.claim()
        for _ in range(2):
            self.ok("begun")
            self.ok("progress", name="start", text=PICK)
        [running] = [b for _, b in self.comments(ID) if b.startswith(f"Run {SID}")]
        self.assertRegex(running, rf"\ARun {SID} · running · #1 · \S+\Z")
        researcher = ACCOUNTS["researcher"]
        self.assertEqual(
            self.stored(),
            [(HARNESS_EMAIL, SESSION), (researcher, comment_id("researcher", SID, "start"))],
        )
        self.assertEqual(self.comments(ID)[-1], (researcher, f"Research started: {PICK}"))
        self.assertEqual(self.ledger(ID), {SID: ["running:1", "start"]})
        # not found: the session comment's first write, the start's lookup before its post
        self.assertEqual(
            self.problems(),
            [
                ("sessions.M_UPDATE", HARNESS_EMAIL, "error"),
                ("linear.Q_COMMENT", researcher, "error"),
            ],
        )

    def test_an_old_start_row_key_writes_as_the_current_role_app(self):
        self.claim(key="deleted-member-key")
        self.ok("progress", name="start", text=PICK)
        self.assertEqual(
            self.stored(), [(ACCOUNTS["researcher"], comment_id("researcher", SID, "start"))]
        )

    def test_no_start_row_exits_2_and_writes_nothing(self):
        res = self.feed("begun")
        self.assertEqual((res.returncode, res.stdout), (2, ""))
        self.assertRegex(
            res.stderr, rf"ensemble-orchestrator writeback: {re.escape(ID)}: no start row"
        )
        self.assertEqual((self.requests(), self.ledger(ID)), ([], {}))


RESEARCH = {
    "status": "done",
    "title": "Queue options",
    "summary": "Three queues compared.",
    "questions": [],
    "url": DOC,
    "files": [],
    "deliverable": "",
}


class Finished(Writeback):
    def test_an_outcome_is_written_back_once_however_often_it_arrives(self):
        self.claim()
        self.ok("begun")
        for _ in range(2):
            self.ok("finished", returncode=0, outcome=RESEARCH, error="", limit=None)
        researcher = ACCOUNTS["researcher"]
        self.assertEqual(
            self.stored(),
            [(HARNESS_EMAIL, SESSION), (researcher, comment_id("researcher", SID, "comment"))],
        )
        self.assertEqual(self.comments(ID)[-1], (researcher, f"Three queues compared.\n\n{DOC}"))
        [session] = [b for _, b in self.comments(ID) if b.startswith(f"Run {SID}")]
        self.assertRegex(session, rf"\ARun {SID} · done · #2 · \S+ → \S+ · exit 0\Z")
        self.assertEqual(self.attachments(ID), [{"url": DOC, "title": "Queue options"}])
        self.assertEqual(self.history(ID), [("in_progress", "in_review", researcher)])
        self.assertEqual(
            self.ledger(ID),
            {SID: ["running:1", "comment", f"attach:{DOC}", "move:in_review", "outcome"]},
        )

    def test_no_outcome_reports_the_error_and_returns_first_research_to_todo(self):
        self.claim()
        self.ok("begun")
        self.ok("finished", returncode=1, outcome=None, error="the client exited 1", limit=None)
        [session] = [b for _, b in self.comments(ID) if b.startswith(f"Run {SID}")]
        self.assertRegex(session, rf"\ARun {SID} · interrupted · #2 · \S+ → \S+ · exit 1\Z")
        researcher = ACCOUNTS["researcher"]
        self.assertEqual(
            (self.state(ID), self.history(ID)), ("todo", [("in_progress", "todo", researcher)])
        )
        self.assertIn("the client exited 1", self.comments(ID)[-1][1])
        self.assertEqual(
            self.ledger(ID),
            {
                SID: [
                    "running:1",
                    "no-outcome:1:comment",
                    "no-outcome:1:move:todo",
                    "no-outcome:1:outcome",
                ]
            },
        )


class Limit(Writeback):
    def limit_rows(self):
        return [r for r in self.runs() if r["kind"] == "limit"]

    def test_a_limit_moves_once_per_attempt_with_one_comment_per_sid(self):
        self.claim()
        limit = {"returncode": -9, "outcome": None, "error": "", "limit": {"resets_at": None}}
        self.ok("begun")
        for _ in range(2):
            self.ok("finished", **limit)
        researcher = ACCOUNTS["researcher"]
        [session] = [b for _, b in self.comments(ID) if b.startswith(f"Run {SID}")]
        self.assertRegex(session, rf"\ARun {SID} · limit · #2 · \S+ → \S+ · exit -9\Z")
        self.assertEqual(self.history(ID), [("in_progress", "in_review", researcher)])
        row = {
            "kind": "limit",
            "issue": ID,
            "sid": SID,
            "client": "claude",
            "resets_at": None,
            "delegate_kind": "harness",
            "attempt": 1,
        }
        self.assertEqual(self.limit_rows(), [row])

        self.ok("begun", attempt=2)
        self.assertEqual(self.state(ID), "in_progress")
        self.ok("finished", attempt=2, **limit)
        self.assertEqual(
            self.history(ID),
            [
                ("in_progress", "in_review", researcher),
                ("in_review", "in_progress", researcher),
                ("in_progress", "in_review", researcher),
            ],
        )
        self.assertEqual(
            self.stored(),
            [(HARNESS_EMAIL, SESSION), (researcher, comment_id("researcher", SID, "limit"))],
        )
        self.assertEqual(self.limit_rows(), [row, {**row, "attempt": 2}])


if __name__ == "__main__":
    unittest.main()
