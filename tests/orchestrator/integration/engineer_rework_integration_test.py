"""An engineer rework from PR comments end to end, as v1 (`v1-runtime`'s core/team/roles/
engineer.md: the user's own PR comments and reviews, `repo.py status`'s `user`, are requirements;
core/team/tasks/light-build.md: a finished build and a user requirement start a new build): a built
issue's user comments on its PR and moves it back to Todo; bin/ensemble-orchestrator router starts a new
session on the same branch, whose agent (fake_claude) runs the team's repo.py, which reads the PR's
comments through fake_gh; its outcome is written back once more.
"""

import json
import os
import re
import sys
import unittest

from tests.delegate.replies import block, mark
from tests.delegate.workdir import checkout_root
from tests.orchestrator.case import REPO as WORKTREE
from tests.orchestrator.integration import fake_gh
from tests.orchestrator.integration.flow_fixtures import (
    ACCOUNTS,
    HUMAN,
    PROJECT,
    REPO,
    UPDRAFT,
    BinFlow,
)

ENGINEER = ACCOUNTS["engineer"]
ID, TITLE, BRANCH = "TASK-7", "ENG: Session registry", "TASK-7-session-registry"
PR = f"https://github.com/{REPO}/pull/1"
TOOL = os.path.join(WORKTREE, "src", "ensemble", "delegate", "team", "tools", "repo.py")
LOGIN = "me"
ASK = "Rename the registry to SessionIndex."
REVIEW = "Looks fine to me."
SAID = "See my comment on the PR."
COMMENT = {"created_at": "2026-10-14T10:00:00Z", "user": {"login": LOGIN}, "body": ASK}
REVIEWED = {
    "submitted_at": "2026-10-14T10:05:00Z",
    "user": {"login": "reviewer"},
    "body": REVIEW,
    "state": "COMMENTED",
}
PULL = {
    "number": 1,
    "url": PR,
    "state": "OPEN",
    "baseRefName": "main",
    "isDraft": True,
    "headRefName": BRANCH,
    "isCrossRepository": False,
    "author": {"login": LOGIN},
}


def outcome(summary):
    return block(
        {
            "status": "done",
            "title": "Session registry",
            "summary": summary,
            "url": PR,
            "updraft": UPDRAFT,
        }
    )


class Rework(BinFlow):
    def setUp(self):
        super().setUp()
        fake_gh.bare(os.path.join(self.root, "repos"), *REPO.split("/"), branches=("main",))
        self.fake.issue(
            TITLE,
            identifier=ID,
            delegate=self.ids[ENGINEER],
            project=PROJECT,
            description="Add a session registry.",
        )

    def build(self, steps):
        self.scene(steps)
        res = self.router("--issue", ID)
        self.assertEqual((res.returncode, res.stdout), (0, ""), (res.stderr, self.said()))
        sid = self.runs()[-1]["sid"]
        self.wait_end(sid, ID)
        self.assertEqual(self.state(ID), "in_review")
        return sid

    def ran(self, name):
        """fake_claude's run step result `name`: its exit code and stdout as JSON."""
        with open(os.path.join(self.root, name)) as f:
            done = json.load(f)
        self.assertEqual(done["returncode"], 0, done["stderr"])
        return json.loads(done["stdout"])

    def repo_py(self, command, name):
        return {
            "kind": "run",
            "argv": [
                sys.executable,
                "-P",
                TOOL,
                command,
                "--dir",
                checkout_root(os.path.join(self.workdirs, ID)),
                "--branch",
                BRANCH,
                "--name",
                ID,
                REPO,
            ],
            "path": os.path.join(self.root, name),
        }

    def section(self, text, heading):
        m = re.search(rf"^## {re.escape(heading)}\n\n(.*?)(?=^## |\Z)", text, re.M | re.S)
        self.assertIsNotNone(m, f"no `## {heading}` in:\n{text}")
        return m[1] if m else ""

    def test_the_users_pr_comments_reach_a_new_build_on_the_same_branch(self):
        """v1 engineer rework: the user's PR comment and Linear move to Todo start a new session
        whose `repo.py status` gives the PR comment as `user`, a review as `others`.
        """
        first = self.build([mark("start", "Building."), outcome("Added the registry.")])
        self.gh.update(
            login=LOGIN,
            pulls={REPO: [PULL]},
            threads={REPO: {"issues/1/comments": [COMMENT], "pulls/1/reviews": [REVIEWED]}},
        )
        self.write_gh()
        self.fake.comment(ID, self.ids[HUMAN], SAID)
        self.fake.move(ID, "todo", actor=self.ids[HUMAN])

        second = self.build(
            [
                mark("start", "Reworking."),
                self.repo_py("worktree", "worktree.json"),
                self.repo_py("status", "status.json"),
                outcome("Renamed the registry."),
            ]
        )
        self.assertEqual(
            [(r["kind"], r["sid"]) for r in self.runs()], [("start", first), ("start", second)]
        )
        self.assertNotEqual(first, second)
        records = self.records(ID)
        at = next(i for i, r in enumerate(records) if r["kind"] == "session" and r["sid"] == second)
        text = next(r["text"] for r in records[at:] if r["kind"] == "input")
        for line in (f"Repo: {REPO}", f"Checkout: {ID}", f"Branch: {BRANCH}"):
            self.assertIn(line, text.splitlines())
        self.assertIn(SAID, self.section(text, "The user's requirements since the last build"))

        worktree = self.ran("worktree.json")
        self.assertEqual(worktree["branch"], BRANCH)
        self.assertTrue(os.path.isdir(worktree["worktree"]), worktree)
        status = self.ran("status.json")
        self.assertEqual(
            status["pr"], {k: PULL[k] for k in ("number", "url", "state", "baseRefName", "isDraft")}
        )
        self.assertEqual(
            status["user"],
            [{"at": COMMENT["created_at"], "kind": "comment", "author": LOGIN, "body": ASK}],
        )
        self.assertEqual(
            status["others"],
            [
                {
                    "at": REVIEWED["submitted_at"],
                    "kind": "review",
                    "author": "reviewer",
                    "body": REVIEW,
                    "state": "COMMENTED",
                }
            ],
        )

        self.assertEqual(
            [body for author, body in self.comments(ID) if author == ENGINEER],
            [
                "Build started: Building.",
                "Build ready:\nAgent report before delivery:\n\n> Added the registry.",
                "Build started: Reworking.",
                "Build ready:\nAgent report before delivery:\n\n> Renamed the registry.",
            ],
        )
        self.assertEqual(self.attachments(ID), [{"url": PR, "title": "Session registry"}])
        self.assertEqual(self.ledger(ID)[second][-1], "outcome")


if __name__ == "__main__":
    unittest.main()
