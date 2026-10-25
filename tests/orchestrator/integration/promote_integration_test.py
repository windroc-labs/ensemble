"""promote.py as a subprocess over FakeLinear: a Handoff issue becomes the next role's issue (its
client id), related, the source Done; a failed move or a Linear outage is retried without a second
child; the tick's prune closes a finished issue's TUI session on a private tmux server, deletes its
agent dir and archives it.
"""

import os
import unittest
from datetime import UTC, datetime, timedelta

from ensemble import delegate
from ensemble.delegate.engine.config import read_config
from ensemble.orchestrator.engine import runs
from ensemble.orchestrator.trackers.linear import ops
from ensemble.session import Session
from tests.delegate.fakes.delegate import FakeDelegate
from tests.delegate.workdir import checkout_path
from tests.orchestrator.config_fixtures import HARNESS_DELEGATE
from tests.orchestrator.integration import fake_linear
from tests.orchestrator.integration.flow_fixtures import (
    ACCOUNTS,
    HARNESS_EMAIL,
    HUMAN,
    PROJECT,
    TIMEOUT,
    Flow,
    TmuxFlow,
)
from tests.utils.sandbox import git_run

SOURCE, TITLE = "TASK-12", "Session registries in agent harnesses"
INSTRUCTIONS = "Write the PRD for a session registry."
MUTATIONS = {op for op, text in fake_linear.texts().items() if text.startswith("mutation")}
WRITES = ["promote.M_CREATE", "promote.M_RELATE", "linear.M_STATE", "linear.M_COMMENT"]
DONE, OPEN = "TASK-20", "TASK-21"
TUI = f"engineer-{DONE}-0123abcd"
CLONE = ("acme", "widgets")


class Tick(Flow):
    def tick(self):
        res = self.promote()
        self.assertEqual((res.returncode, res.stderr, res.stdout), (0, "", ""))

    def events(self):
        return [{k: v for k, v in e.items() if k != "ts"} for e in self.logged()]

    def outage(self, op):
        res = self.promote()
        self.assertEqual((res.returncode, res.stdout, res.stderr), (1, "", ""))
        self.assertEqual(self.requests()[-1].result, "503")
        self.assertEqual(
            self.events(), [{"src": "promote", "kind": "linear-error", "op": op, "status": 503}]
        )

    def assert_requests(self, writes, problems=()):
        self.assertEqual(self.problems(), list(problems))
        requests = self.requests()
        self.assertEqual({r.account for r in requests}, {HARNESS_EMAIL})
        self.assertEqual([r.op for r in requests if r.op in MUTATIONS], writes)


class Promote(Tick):
    def setUp(self):
        super().setUp()
        now, researcher, human = (
            datetime.now(UTC),
            self.ids[ACCOUNTS["researcher"]],
            self.ids[HUMAN],
        )
        self.fake.issue(
            TITLE,
            identifier=SOURCE,
            state="in_progress",
            delegate=researcher,
            assignee=human,
            project=PROJECT,
        )
        self.fake.move(SOURCE, "in_review", actor=researcher, at=now - timedelta(minutes=40))
        self.fake.comment(SOURCE, human, INSTRUCTIONS, at=now - timedelta(minutes=30))
        self.fake.move(SOURCE, "handoff", actor=human, at=now - timedelta(minutes=20))
        with self.fake.lock:
            source = self.fake.find(SOURCE)
            (said,), handoff = source["comments"], source["history"][-1]
            self.source, self.url, name = (
                source["id"],
                source["url"],
                self.fake.users[human]["name"],
            )
            self.instructions = f"## Instructions\n{name}, {said['createdAt']}:\n{INSTRUCTIONS}"
            self.child = ops.client_id(source["id"], "pm", handoff["createdAt"])

    def assert_promoted(self):
        with self.fake.lock:
            child = self.fake.find(self.child)
            self.assertIsNotNone(child, f"no issue {self.child}")
            child, relations = dict(child), list(self.fake.relations)
        self.assertEqual(
            (
                self.state(self.child),
                child["delegate"],
                child["assignee"],
                child["project"],
                child["title"],
            ),
            ("todo", self.ids[ACCOUNTS["pm"]], None, PROJECT, f"PRD: {TITLE}"),
        )
        self.assertEqual(
            child["description"].split("\n\n")[:2],
            [f"Handoff from {SOURCE}: {self.url}", self.instructions],
        )
        self.assertEqual(relations, [("related", self.source, self.child)])
        self.assertEqual(self.state(SOURCE), "done")
        self.assertEqual(
            self.history(SOURCE),
            [
                ("in_progress", "in_review", ACCOUNTS["researcher"]),
                ("in_review", "handoff", HUMAN),
                ("handoff", "done", HARNESS_EMAIL),
            ],
        )
        self.assertEqual(
            self.comments(SOURCE),
            [(HUMAN, INSTRUCTIONS), (HARNESS_EMAIL, f"Promoted to {child['identifier']}.")],
        )
        return child["identifier"]

    def test_handoff_to_a_child_issue(self):
        self.tick()
        child = self.assert_promoted()
        self.assert_requests(WRITES)
        self.assertEqual(
            self.events(), [{"src": "promote", "kind": "promote", "issue": SOURCE, "to": child}]
        )

    def assert_move_retried(self, failed):
        with self.fake.lock:
            self.assertIsNotNone(self.fake.find(self.child), f"no issue {self.child}")
            self.assertEqual(self.fake.relations, [("related", self.source, self.child)])
        self.assertEqual(self.state(SOURCE), "handoff")
        self.assertEqual(self.comments(SOURCE), [(HUMAN, INSTRUCTIONS)])
        self.assert_requests(WRITES[:3], failed)
        before = self.events()

        self.tick()
        child = self.assert_promoted()
        self.assertEqual(
            self.events(),
            [*before, {"src": "promote", "kind": "promote", "issue": SOURCE, "to": child}],
        )
        self.assert_requests(WRITES[:3] + WRITES[2:], failed)

    def test_a_failed_move_is_retried_without_a_second_child(self):
        self.fake.fail("linear.M_STATE", "error", account=HARNESS_EMAIL)
        self.tick()
        [error] = self.events()
        self.assertEqual(
            (error["src"], error["kind"], error["issue"]), ("promote", "handoff-error", SOURCE)
        )
        self.assertIn("linear.M_STATE: injected error", error["error"])
        self.assert_move_retried([("linear.M_STATE", HARNESS_EMAIL, "error")])

    def test_a_linear_outage_moving_the_source_is_retried_without_a_second_child(self):
        self.fake.fail("linear.M_STATE", "503", account=HARNESS_EMAIL)
        self.outage("issueUpdate")
        self.assert_move_retried([("linear.M_STATE", HARNESS_EMAIL, "503")])

    def test_a_linear_outage_reading_the_handoffs_is_retried(self):
        self.fake.fail("promote.Q_HANDOFF", "503")
        self.outage("issues")
        self.assertEqual(self.state(SOURCE), "handoff")
        self.assert_requests([], [("promote.Q_HANDOFF", HARNESS_EMAIL, "503")])

        self.tick()
        child = self.assert_promoted()
        self.assertEqual(
            self.events()[1:], [{"src": "promote", "kind": "promote", "issue": SOURCE, "to": child}]
        )
        self.assert_requests(WRITES, [("promote.Q_HANDOFF", HARNESS_EMAIL, "503")])


class PruneSessionDelegate(FakeDelegate):
    ROLES = {
        "engineer": {"build": "", "light-build": ""},
        "researcher": {"deep-research": "", "light-research": ""},
        "pm": {"product-design": ""},
    }

    @classmethod
    def resolve(cls, role, task=None, *, overlays=()):
        return HARNESS_DELEGATE.resolve(role, task, overlays=overlays)

    def close(self):
        Session.configure_from(read_config())
        Session.host().attach(self.status().name).close()
        super().close()


class Prune(Tick, TmuxFlow):
    def git_init(self, path):
        res = git_run(
            "init",
            "--quiet",
            path,
            env=self.env(),
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_prune_after_done(self):
        engineer = self.ids[ACCOUNTS["engineer"]]
        done_id = self.fake.issue(
            "ENG: Session registry",
            identifier=DONE,
            state="in_review",
            delegate=engineer,
            project=PROJECT,
        )["id"]
        self.fake.move(
            DONE, "done", actor=self.ids[HUMAN], at=datetime.now(UTC) - timedelta(hours=25)
        )
        self.fake.issue(
            "ENG: Session reaper",
            identifier=OPEN,
            state="in_progress",
            delegate=engineer,
            project=PROJECT,
        )
        workdir = os.path.join(self.workdirs, DONE)
        kept = checkout_path(os.path.join(self.workdirs, OPEN), *CLONE, OPEN)
        for path in (checkout_path(workdir, *CLONE, DONE), kept):
            self.git_init(path)
        made = self.server.tmux("new-session", "-d", "-s", TUI, "cat", "-")
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertTrue(self.live(TUI))
        sid = "0123abcd-1234-4567-89ab-0123456789ab"
        PruneSessionDelegate("engineer", delegate.Spec("", workdir, sid=sid, name=TUI)).detach()
        runs.note(
            os.path.join(self.logs, "runs.jsonl"),
            "start",
            issue=DONE,
            sid=sid,
            role="engineer",
            client="claude",
            delegate_kind="harness",
        )
        local = os.path.join(self.ensemble, "config.local.toml")
        with open(local) as stream:
            text = stream.read()
        with open(local, "w") as stream:
            stream.write(
                text.replace(
                    "ensemble.delegate:HarnessDelegate",
                    "tests.orchestrator.integration.promote_integration_test:PruneSessionDelegate",
                )
            )

        self.tick()
        self.assertIsNone(PruneSessionDelegate.checkouts(workdir), self.events())
        self.assertTrue(os.path.isdir(workdir))
        self.assertFalse(self.live(TUI))
        self.assertTrue(os.path.isdir(os.path.join(kept, ".git")))
        self.assertEqual([self.issue(i)["archived"] for i in (DONE, OPEN)], [True, False])
        self.assertEqual(self.sent("prune.M_ARCHIVE"), [{"i": done_id}])
        self.assert_requests(["prune.M_ARCHIVE"])
        self.assertEqual(
            self.events(),
            [
                {"src": "prune", "kind": "prune-closed", "issue": DONE, "session": sid},
                {"src": "prune", "kind": "prune-removed", "issue": DONE, "what": "agent dir"},
                {"src": "prune", "kind": "prune-archived", "issue": DONE},
            ],
        )


if __name__ == "__main__":
    unittest.main()
