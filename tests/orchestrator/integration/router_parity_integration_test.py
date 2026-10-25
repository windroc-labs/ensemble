"""v1 router behaviors, each against its v1 source (`v1-runtime`'s CLAUDE.md › Architecture or a v1
test, named in the test's docstring), end to end: bin/ensemble-orchestrator router as a real process
(BinFlow), fake_claude as client claude, FakeLinear over HTTP, the runs on a private tmux server.
"""

import os
import re
import signal
import subprocess
import time
import unittest
from datetime import UTC, datetime, timedelta

from ensemble.orchestrator.engine.router import BUSY, LIVE
from tests.delegate.replies import block, mark, option
from tests.delegate.workdir import session_cwd
from tests.orchestrator.integration.flow_fixtures import (
    ACCOUNTS,
    HARNESS_EMAIL,
    HUMAN,
    LIGHT_RESEARCH,
    MANAGER,
    PROJECT,
    REPO,
    TIMEOUT,
    UPDRAFT,
    BinFlow,
    usage,
)
from tests.orchestrator.integration.segments import agent_calls
from tests.orchestrator.unit.board_ids import TASK_GROUP
from tests.session import live_tmux

ENGINEER = ACCOUNTS["engineer"]
ID, OTHER, SOURCE = "TASK-7", "TASK-8", "TASK-12"
STARTED, READY = "Building the session registry.", "Added the session registry."
PR = f"https://github.com/{REPO}/pull/1"
START = mark("start", STARTED)
DONE = block(
    {
        "status": "done",
        "title": "Session registry",
        "summary": READY,
        "url": PR,
        "updraft": UPDRAFT,
    }
)
# a hang answer's delay: the first router holds the lock that long
HOLD = 10
MYSTERY = "00000000-0000-4000-8000-000000000031"


class Parity(BinFlow):
    def seed(self, ident, title, assignee=ENGINEER, **fields):
        self.fake.issue(
            title,
            identifier=ident,
            assignee=self.ids[assignee],
            project=PROJECT,
            description=f"{title}.",
            **fields,
        )

    def tick(self):
        res = self.router()
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""), self.said())

    def routed(self):
        """The router's orchestrator.jsonl lines: a run's write-back may log in between."""
        return [line for line in self.said() if line.startswith("router ")]

    def started(self, n=1):
        """The first n agent runs' fake_claude calls, once logged."""
        return live_tmux.wait(
            lambda: (calls := agent_calls(self.calls()))[n - 1 :] and calls,
            TIMEOUT,
            f"fake claude's start #{n}",
        )


class Lock(Parity):
    def setUp(self):
        super().setUp()
        self.seed(ID, "ENG: Session registry")

    def hold(self):
        """A tick in the router lock, its first Linear call (the team) answered HOLD s late."""
        self.fake.fail("linear.Q_TEAM", "hang", seconds=HOLD)
        self.scene([START, DONE], usage=usage(0.5))
        first = self.start("router")
        live_tmux.wait(self.requests, TIMEOUT, "the first router's team query")
        return first

    def assert_skipped(self, first, said):
        self.assertIsNone(first.poll(), "the first router ended before the second ran")
        self.assertEqual(self.said(), [said])
        self.assertEqual([r.op for r in self.requests()], ["linear.Q_TEAM"])
        self.assertEqual(agent_calls(self.calls()), [])
        out, err = first.communicate(timeout=TIMEOUT)
        self.assertEqual((first.returncode, out, err), (0, "", ""))
        [row] = self.runs()
        self.assertEqual((row["kind"], row["issue"]), ("start", ID))
        self.wait_end(row["sid"], ID)
        self.assertEqual(self.state(ID), "in_review")
        self.assertEqual(self.said().count(said), 1)

    def test_a_tick_while_another_router_holds_the_lock_logs_one_skip_and_does_nothing(self):
        """v1 CLAUDE.md › Architecture: the router lock keeps one router at a time; held by
        another router → one `skip` event, nothing else.
        """
        first = self.hold()
        res = self.router()
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))
        self.assert_skipped(first, f"router skip reason={BUSY}")

    def test_an_issue_run_while_another_router_holds_the_lock_logs_one_skip(self):
        """v1 CLAUDE.md › Architecture: `--issue` takes the router lock too; held → one `skip`."""
        first = self.hold()
        res = self.router("--issue", ID)
        self.assertEqual(
            (res.returncode, res.stdout, res.stderr),
            (2, "", f"router.py: {BUSY}; try again\n"),
        )
        self.assert_skipped(first, f"router skip {ID} reason={BUSY}")


class MaxRuns(Parity):
    def setUp(self):
        super().setUp()
        local = os.path.join(self.ensemble, "config.local.toml")
        with open(local) as f:
            text = f.read()
        table = "[orchestrator.roles.engineer]\n"
        self.assertEqual(text.count(table), 1)
        with open(local, "w") as f:
            f.write(text.replace(table, table + "max_runs = 1\n"))
        self.seed(ID, "ENG: Session registry", priority=2)
        self.seed(OTHER, "ENG: Session index", priority=3)

    def test_a_live_agent_run_on_tmux_holds_the_roles_only_slot(self):
        """v1 CLAUDE.md › Architecture: a tick compares live tmux sessions with `max_runs`; the
        issue's driver session holds a slot while its driver runs, so a full role claims nothing.
        """
        gate = os.path.join(self.root, "gate")
        self.scene([START, DONE], gate=gate, usage=usage(0.5))
        self.tick()
        [first] = self.runs()
        self.assertEqual((first["kind"], first["issue"]), ("start", ID))
        self.started()
        self.assertTrue(self.live(self.harness("engineer", ID, first["sid"])))

        said = self.routed()
        self.tick()
        self.assertEqual((self.routed(), self.runs(), self.state(OTHER)), (said, [first], "todo"))
        self.assertEqual(len(agent_calls(self.calls())), 1)

        with open(gate, "w"):
            pass
        self.wait_end(first["sid"], ID)
        self.tick()
        _, second = self.runs()
        self.assertEqual((second["kind"], second["issue"]), ("start", OTHER))
        self.wait_end(second["sid"], OTHER)
        self.assertEqual((self.state(ID), self.state(OTHER)), ("in_review", "in_review"))


def gone(pid):
    res = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=TIMEOUT
    )
    return res.stdout.strip() == "" or res.stdout.strip().startswith("Z")


class Recover(Parity):
    def setUp(self):
        super().setUp()
        self.seed(ID, "ENG: Session registry")

    def transcript(self, sid):
        """The Claude client's transcript of sid in ID's agent dir and the files beside it."""
        cwd = session_cwd(os.path.join(self.workdirs, ID))
        project = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd))
        path = os.path.join(self.root, "claude-config", "projects", project, f"{sid}.jsonl")
        self.assertTrue(os.path.isfile(path), path)
        beside = os.walk(os.path.splitext(path)[0])
        return [path, *(os.path.join(top, name) for top, _, names in beside for name in names)]

    def interrupted(self):
        """ID's run ended without an outcome by a SIGTERM to its harness, its transcript idle past
        router.LIVE: In Progress, a resumable session no tick holds back. Claimed by --issue: a
        tick's usage probe would hang with the agent.
        """
        self.scene([START], hang=True)
        res = self.router("--issue", ID)
        self.assertEqual(res.returncode, 0, res.stderr)
        [row] = self.runs()
        sid = row["sid"]
        live_tmux.wait(
            lambda: "start" in self.ledger(ID).get(sid, []), TIMEOUT, "write-back's start step"
        )
        [agent] = self.started()
        harness = self.harness("engineer", ID, sid)
        pane = self.server.tmux("list-panes", "-t", f"={harness}", "-F", "#{pane_pid}")
        self.assertEqual(pane.returncode, 0, pane.stderr)
        os.kill(int(pane.stdout), signal.SIGTERM)
        self.wait_end(sid, ID)
        live_tmux.wait(lambda: gone(agent["pid"]), TIMEOUT, f"fake claude {agent['pid']} to end")
        self.assertEqual(
            (self.state(ID), self.ledger(ID)), ("in_progress", {sid: ["running:1", "start"]})
        )
        idle = time.time() - 2 * LIVE.total_seconds()
        for path in self.transcript(sid):
            os.utime(path, (idle, idle))
        return sid

    def test_a_tick_resumes_an_interrupted_in_progress_run(self):
        """v1 CLAUDE.md › Architecture: a tick, without `--issue`, Recovers dead In Progress agent
        runs before it claims (v1 router_integration_test.test_a_crash_mid_run_resumes_the_session
        ran Recover by --issue only).
        """
        sid = self.interrupted()
        self.scene([START, DONE], usage=usage(0.5))
        self.tick()
        self.assertEqual(
            [(r["kind"], r["sid"]) for r in self.runs()], [("start", sid), ("resume", sid)]
        )
        self.assertTrue(
            any(
                line.startswith(f"router plan {ID} mode=resume sid={sid}") for line in self.routed()
            ),
            self.routed(),
        )
        self.wait_end(sid, ID, ends=2)
        first, second = self.started(2)
        self.assertEqual(option(first, "--session-id"), sid)
        self.assertEqual((option(second, "--resume"), option(second, "--session-id")), (sid, None))
        self.assertEqual(self.state(ID), "in_review")

    def test_a_logged_role_not_the_assignees_is_not_resumed_and_goes_to_in_review(self):
        """v1 CLAUDE.md › Architecture: Recover resumes a session only while its runs.jsonl
        `role` is the assignee's (v1 router_test.test_fr22_a_logged_role_not_the_assignees_goes_to_review).
        """
        sid = self.interrupted()
        with self.fake.lock:
            self.fake.find(ID)["assignee"] = self.ids[ACCOUNTS["pm"]]
        self.scene([START, DONE], usage=usage(0.5))
        self.tick()
        self.assertEqual([(r["kind"], r["sid"]) for r in self.runs()], [("start", sid)])
        self.assertEqual(len(agent_calls(self.calls())), 1)
        why = "role (engineer) is not the assignee's (pm)"
        self.assertIn(f"router recover {ID} to=in_review reason={why}", self.routed())
        self.assertEqual(self.state(ID), "in_review")
        self.assertEqual(self.history(ID)[-1], ("in_progress", "in_review", HARNESS_EMAIL))
        author, body = self.comments(ID)[-1]
        self.assertEqual(author, HARNESS_EMAIL)
        self.assertTrue(body.startswith(f"The interrupted agent run's {why}; needs a look."), body)
        self.assertEqual(self.subscribers(ID), [HUMAN])


class Refusals(Parity):
    def assert_nothing_started(self):
        self.assertEqual((self.runs(), agent_calls(self.calls())), ([], []))
        sessions = self.server.tmux("list-sessions", "-F", "#{session_name}")
        self.assertEqual(sessions.stdout.split(), [MANAGER], sessions.stderr)

    def test_a_new_run_whose_target_check_fails_is_bounced_and_starts_no_agent_run(self):
        """v1 CLAUDE.md › Architecture: router.outer, a build role: `target.check` Invalid on a
        new agent run → `writeback.bounce`, no agent run; the bounce acts as the role's account
        and its move to In Review subscribes each human_members email.
        """
        self.seed(ID, "ENG: Session registry")
        self.gh["repos"][REPO]["permissions"]["push"] = False
        self.write_gh()
        self.scene([START, DONE], usage=usage(0.5))
        self.tick()
        self.assert_nothing_started()
        self.assertEqual(self.state(ID), "in_review")
        self.assertEqual(
            self.history(ID),
            [("todo", "in_progress", HARNESS_EMAIL), ("in_progress", "in_review", ENGINEER)],
        )
        [(author, body)] = self.comments(ID)
        self.assertEqual(author, ENGINEER)
        self.assertIn("repo check failed: ", body)
        self.assertIn(f"{REPO}: no push permission", body)
        self.assertEqual(self.subscribers(ID), [HUMAN])
        [bounce] = [line for line in self.routed() if line.startswith(f"router bounce {ID} ")]
        self.assertTrue(bounce.endswith(f"{REPO}: no push permission"), bounce)
        self.assertIn(f"router launch {ID} exit=0", self.routed())

    def test_a_promoted_issue_whose_target_check_fails_returns_to_its_source(self):
        """v1 writeback.bounce of an issue promoted from a Handoff source in the team: the
        source is unarchived, gets the `Repo check failed:` comment and goes Done → In Review;
        the issue gets it too and is Canceled; no agent run.
        """
        now, pm = datetime.now(UTC), self.ids[ACCOUNTS["pm"]]
        self.fake.issue(
            "PRD: Session registry",
            identifier=SOURCE,
            state="in_progress",
            assignee=pm,
            project=PROJECT,
        )
        self.fake.move(SOURCE, "in_review", actor=pm, at=now - timedelta(minutes=40))
        self.fake.comment(SOURCE, self.ids[HUMAN], "Build it.", at=now - timedelta(minutes=30))
        self.fake.move(SOURCE, "handoff", actor=self.ids[HUMAN], at=now - timedelta(minutes=20))
        res = self.promote()
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""), self.said())
        [child] = [
            line.rpartition(" to=")[2]
            for line in self.said()
            if line.startswith(f"promote promote {SOURCE} ")
        ]
        self.assertEqual(self.state(SOURCE), "done")
        self.gh["repos"][REPO]["permissions"]["push"] = False
        self.write_gh()
        self.scene([START, DONE], usage=usage(0.5))
        self.tick()
        self.assert_nothing_started()
        self.assertEqual((self.state(SOURCE), self.state(child)), ("in_review", "canceled"))
        self.assertEqual(self.history(SOURCE)[-1], ("done", "in_review", ENGINEER))
        self.assertEqual(self.history(child)[-1], ("in_progress", "canceled", ENGINEER))
        for ident in (SOURCE, child):
            with self.subTest(ident):
                author, body = self.comments(ident)[-1]
                self.assertEqual(author, ENGINEER)
                self.assertTrue(body.startswith("Repo check failed: "), body)
                self.assertIn(f"{REPO}: no push permission", body)
                self.assertIn(f"move {SOURCE} to Handoff again", body)
        self.assertIn(HUMAN, self.subscribers(SOURCE))

    def test_bad_task_labels_send_their_issues_to_in_review_unclaimed(self):
        """v1 CLAUDE.md › Architecture and v1 router_test.test_fr7_invalid_label_goes_to_review:
        a Tasks label that is not one of the role's tasks, or not in [linear.task_labels], is a
        claim-skip: the issue goes to In Review with a comment saying what to fix, no agent run.
        """
        self.fake.label_group(TASK_GROUP, "Tasks", {MYSTERY: "Mystery"})
        self.seed(ID, "ENG: Session registry", labels=[LIGHT_RESEARCH])
        self.seed(OTHER, "ENG: Session index", labels=[MYSTERY])
        self.scene([START, DONE], usage=usage(0.5))
        self.tick()
        self.assert_nothing_started()
        for ident, said in (
            (ID, 'Task "light-research" is not one of engineer\'s tasks ('),
            (OTHER, 'Task label "Mystery" is not in '),
        ):
            with self.subTest(ident):
                self.assertIn(
                    f"router claim-skip {ident} to=in_review reason=bad task label", self.routed()
                )
                self.assertEqual(self.state(ident), "in_review")
                self.assertEqual(self.history(ident), [("todo", "in_review", HARNESS_EMAIL)])
                [(author, body)] = self.comments(ident)
                self.assertEqual(author, HARNESS_EMAIL)
                self.assertTrue(body.startswith(said), body)
                self.assertIn("then move the issue back to Todo.", body)
                self.assertEqual(self.subscribers(ident), [HUMAN])


if __name__ == "__main__":
    unittest.main()
