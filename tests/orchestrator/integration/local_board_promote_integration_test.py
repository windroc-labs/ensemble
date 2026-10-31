"""Local issues from review to archive through bin/ensemble-orchestrator router, promote and prune
as real processes with tracker = "local".
"""

import os
import re
import unittest
from datetime import UTC, datetime, timedelta

from ensemble.orchestrator.engine.config import BOARD_ENV
from ensemble.orchestrator.trackers.local.local_tracker import LocalTracker
from tests.delegate.replies import block
from tests.orchestrator.integration.flow_fixtures import BinFlow

REPORT = "https://github.com/windroc/team-docs/blob/main/Research/queues.md"
OUTCOME = {"status": "done", "title": "Queue survey", "summary": "Compared.", "url": REPORT}
ROLES = ("researcher", "pm", "engineer")
PM, RESEARCH = "LOC-5 Queue survey.md", "LOC-1 Queue survey.md"
HISTORY = re.compile(r"- (\S+ \S+) (\w+) (self|other)\n")


def ago(**delta):
    """A History time as the processes (TZ=UTC) write it."""
    return (datetime.now(UTC) - timedelta(**delta)).strftime("%Y-%m-%d %H:%M")


def issue(ident, *, front="", history=""):
    return (
        f'---\nid: {ident}\ntitle: "Queue survey"\ncreated: 2026-10-20T09:00:00+00:00\n{front}---\n'
        f"### Description\nSurvey the queues.\n### History\n{history}"
    )


class ReviewToArchive(BinFlow):
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
        self.reviewed = "".join(
            f"- {ago(hours=h)} {s}\n"
            for h, s in ((3, "ready other"), (2, "running self"), (1, "review self"))
        )
        self.put(f"pm/review/{PM}", issue("LOC-5", front="parent: LOC-8\n", history=self.reviewed))
        self.put("pm/backlog/LOC-3 Sub.md", issue("LOC-3", front="parent: LOC-5\n"))
        self.put(f"researcher/ready/{RESEARCH}", issue("LOC-1", front="parent: LOC-9\n"))
        self.put(".next-id", "10\n")

    def bin_env(self):
        return {**super().bin_env(), BOARD_ENV: self.board}

    def at(self, rel):
        return os.path.join(self.board, rel)

    def put(self, rel, text):
        os.makedirs(os.path.dirname(self.at(rel)), exist_ok=True)
        with open(self.at(rel), "w", encoding="utf-8") as f:
            f.write(text)

    def read(self, rel):
        with open(self.at(rel), encoding="utf-8") as f:
            return f.read()

    def move(self, rel, to, *, age=None):
        """A person's move of rel into the state dir to, its mtime age ago when given."""
        os.makedirs(self.at(to), exist_ok=True)
        os.rename(self.at(rel), target := os.path.join(self.at(to), os.path.basename(rel)))
        if age is not None:
            self.age(os.path.relpath(target, self.board), age)
        return os.path.relpath(target, self.board)

    def age(self, rel, age):
        when = (datetime.now(UTC) - age).timestamp()
        os.utime(self.at(rel), (when, when))

    def history(self, rel):
        return [m.groups()[1:] for m in HISTORY.finditer(self.read(rel))]

    def files(self):
        """Every board file but its state root's, by path."""
        found = {}
        for folder, dirs, names in os.walk(self.board):
            dirs[:] = [d for d in dirs if d != ".state"]
            for rel in (os.path.relpath(os.path.join(folder, n), self.board) for n in names):
                found[rel] = self.read(rel)
        return found

    def run_bin(self, *argv):
        res = self.linear(*argv)
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""), self.said())

    def test_review_to_archive(self):
        with self.subTest("router: a researcher's run reaches review"):
            self.scene([block(OUTCOME)])
            self.run_bin("router", "--issue", "LOC-1")
            [row] = self.runs()
            self.wait_end(row["sid"], "LOC-1", role="researcher")
            self.assertTrue(os.path.exists(self.at(f"researcher/review/{RESEARCH}")))
            self.assertTrue(os.path.isdir(os.path.join(self.workdirs, "LOC-1")))

        with self.subTest("approved, inside the undo window: promote waits"):
            text = self.read(f"researcher/review/{RESEARCH}")  # its run back-dated an hour or more
            said = (
                f"\n#### other · {ago(minutes=30)}\nWrite the PRD.\n"  # the person's instructions
            )
            head = text[: text.index("\n### History\n")]
            self.put(f"researcher/review/{RESEARCH}", f"{head}{said}\n### History\n{self.reviewed}")
            research = self.move(f"researcher/review/{RESEARCH}", "researcher/approved")
            pm = self.move(f"pm/review/{PM}", "pm/approved")
            before = self.files()
            self.run_bin("promote")
            self.assertEqual(self.files(), before)
            for ident in ("LOC-5", "LOC-1"):
                self.assertIn(
                    f"promote handoff-wait {ident} reason=handoff age under 10 min", self.said()
                )

        with self.subTest("past the undo window: promote makes each next issue once"):
            self.age(pm, timedelta(minutes=12))
            self.age(research, timedelta(minutes=11))
            self.run_bin("promote")
            self.assertEqual(os.listdir(self.at("engineer/ready")), ["LOC-10 ENG: Queue survey.md"])
            self.assertEqual(os.listdir(self.at("pm/ready")), ["LOC-11 PRD: Queue survey.md"])
            for rel, parent in (
                ("engineer/ready/LOC-10 ENG: Queue survey.md", "LOC-8"),
                ("pm/ready/LOC-11 PRD: Queue survey.md", "LOC-9"),
            ):
                text = self.read(rel)
                self.assertIn(f"\nparent: {parent}\n", text)
                self.assertIn("\nCreated by promote.\n", text)
                self.assertEqual(self.history(rel), [("ready", "self")])
            pm, research = f"pm/done/{PM}", f"researcher/done/{RESEARCH}"
            for rel, child in ((pm, "LOC-10"), (research, "LOC-11")):
                self.assertIn(f"\nrelated: [{child}]\n", self.read(rel))
                self.assertEqual(self.history(rel)[-2:], [("approved", "other"), ("done", "self")])
            detail = LocalTracker(self.board, ROLES, prefix="LOC").detail("LOC-5")
            self.assertEqual(detail.comments[-1].body, "Promoted to LOC-10.")
            self.assertEqual(self.read(".next-id"), "12\n")
            before = self.files()
            self.run_bin("promote")
            self.assertEqual(self.files(), before)

        with self.subTest("done, a sub-issue open: prune cleans the run, archives nothing"):
            done = "".join(  # both done three hours back: their History as one past timeline
                f"- {ago(minutes=m)} {s}\n"
                for m, s in (
                    (360, "ready other"),
                    (300, "running self"),
                    (240, "review self"),
                    (190, "approved other"),
                    (180, "done self"),
                )
            )
            for rel in (pm, research):
                text = self.read(rel)
                self.assertEqual(self.history(rel)[-1], ("done", "self"))
                self.put(rel, text[: text.index("### History\n")] + f"### History\n{done}")
            self.run_bin("prune")
            said = self.said()
            self.assertIn(f"prune prune-closed LOC-1 session={row['sid']}", said)
            self.assertIn("prune prune-removed LOC-1 what=agent dir", said)
            self.assertIn(
                "prune prune-skip LOC-5 reason=archive: sub-issue LOC-3 not finished", said
            )
            self.assertFalse(os.path.exists(self.at("archive")))

        with self.subTest("the sub-issue done: prune archives the issue and it flat"):
            self.move("pm/backlog/LOC-3 Sub.md", "pm/done", age=timedelta(hours=3))
            self.run_bin("prune")
            self.assertIn("prune prune-archived LOC-5", self.said())
            self.assertEqual(sorted(os.listdir(self.at("archive"))), ["LOC-3 Sub.md", PM])
            self.assertFalse(os.path.exists(self.at(pm)))
            self.assertRegex(
                self.read(f"archive/{PM}"),
                r"<!-- id=c\d+ key=archived:pm -->\nArchived from pm/done/\.\n",
            )
            self.assertEqual(self.history(f"archive/{PM}")[-1], ("done", "self"))
            self.assertTrue(os.path.exists(self.at(research)))  # a researcher's report stays
            self.assertTrue(os.path.exists(self.at("engineer/ready/LOC-10 ENG: Queue survey.md")))


if __name__ == "__main__":
    unittest.main()
