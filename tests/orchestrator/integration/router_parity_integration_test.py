"""v1 router behaviors end to end (BinFlow); a test's docstring names its v1 source."""

import os
import re
import time
import unittest
from datetime import UTC, datetime, timedelta

from ensemble.orchestrator.engine.router import BUSY, LIVE, STARTING, WAIT
from tests.delegate.replies import block, mark, option
from tests.delegate.workdir import session_cwd
from tests.orchestrator.integration.flow_fixtures import (
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
from tests.orchestrator.unit.board_ids import HARNESS_USER, TASK_GROUP, role_id
from tests.session import live_tmux
from tests.utils.sandbox import gone

ENGINEER = role_id("engineer")
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
# a hang answer's longest delay (2 * HOLD where the test releases it): a router's lock hold
HOLD = 10
MYSTERY = "00000000-0000-4000-8000-000000000031"


class Parity(BinFlow):
    def seed(self, ident, title, delegate=ENGINEER, **fields):
        self.fake.issue(
            title,
            identifier=ident,
            delegate=delegate,
            assignee=self.ids[HUMAN],
            project=PROJECT,
            description=f"{title}.",
            **fields,
        )

    def tick(self):
        res = self.router()
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""), self.said())

    def routed(self):
        """A run's write-back may log in between."""
        return [line for line in self.said() if line.startswith("router ")]

    def started(self, n=1):
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
        self.fake.fail("linear.Q_TEAM", "hang", seconds=2 * HOLD)
        self.scene([START, DONE], usage=usage(0.5))
        first = self.start("router")
        live_tmux.wait(self.requests, TIMEOUT, "the first router's team query")
        return first

    def test_a_held_lock_skips_a_tick_once_and_an_issue_run_waits_then_starts(self):
        """One router at a time: a tick skips; `--issue` waits (up to WAIT s), then starts."""
        self.seed(OTHER, "ENG: Session index")
        first, skip = self.hold(), f"router skip reason={BUSY}"
        res = self.router()
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))
        self.assertIsNone(first.poll(), "the first router ended before the second ran")
        self.assertEqual(self.said(), [skip])
        self.assertEqual([r.op for r in self.requests()], ["linear.Q_TEAM"])
        self.assertEqual(agent_calls(self.calls()), [])
        second = self.start("router", "--issue", OTHER)
        waiting = f"router.py: {BUSY}; waiting up to {WAIT} s\n"
        self.assertEqual(next((x for x in second.stderr if x == waiting), None), waiting)
        self.fake.release()
        out, err = first.communicate(timeout=TIMEOUT)
        self.assertEqual((first.returncode, out, err), (0, "", ""))
        self.all_started([second], [ID, OTHER])
        self.assertEqual(len(self.runs()), 2)
        self.assertEqual([x for x in self.said() if f"reason={BUSY}" in x], [skip])

    def inside_start(self):
        """ID's router, past its claim: its issue read answered on release (2 * HOLD s at most)."""
        self.fake.fail("issues.Q_ISSUE", "hang", seconds=2 * HOLD)
        self.scene([START, DONE], usage=usage(0.5))
        first = self.start("router", "--issue", ID)
        live_tmux.wait(
            lambda: "issues.Q_ISSUE" in [r.op for r in self.requests()],
            TIMEOUT,
            "the first router's issue read",
        )
        return first

    def all_started(self, procs, idents):
        errs = []
        for proc in procs:
            _, err = proc.communicate(timeout=TIMEOUT)
            self.assertEqual(proc.returncode, 0, err)
            errs.append(err)
        rows = {row["issue"]: row for row in self.runs()}
        self.assertEqual(sorted(rows), sorted(idents))
        self.assertEqual({row["kind"] for row in rows.values()}, {"start"})
        for ident, row in rows.items():
            self.wait_end(row["sid"], ident)
        self.assertEqual({self.state(i) for i in idents}, {"in_review"})
        return errs

    def test_parallel_issue_runs_of_different_issues_all_start(self):
        idents = [ID, OTHER, SOURCE]
        for ident in idents[1:]:
            self.seed(ident, f"ENG: Session part {ident}")
        self.fake.fail("linear.Q_TEAM", "hang", seconds=HOLD)
        self.scene([START, DONE], usage=usage(0.5))
        procs = [self.start("router", "--issue", ident) for ident in idents]
        errs = self.all_started(procs, idents)
        waited = [e for e in errs if f"router.py: {BUSY}; waiting up to {WAIT} s\n" in e]
        self.assertEqual(len(waited), 2, errs)
        self.assertEqual(len(agent_calls(self.calls())), 3)
        self.assertNotIn(f"reason={BUSY}", "\n".join(self.said()))

    def test_an_issue_run_starts_while_another_is_inside_its_start(self):
        self.seed(OTHER, "ENG: Session index")
        first = self.inside_start()
        res = self.router("--issue", OTHER)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIsNone(first.poll(), "the first router ended before the second")
        self.fake.release()
        self.all_started([first], [ID, OTHER])

    def test_a_second_run_of_the_same_issue_is_refused_while_the_first_starts(self):
        first = self.inside_start()
        res = self.router("--issue", ID)
        self.assertEqual(
            (res.returncode, res.stdout, res.stderr),
            (2, "", f"router.py: {ID} is {STARTING}; try again\n"),
        )
        self.assertIsNone(first.poll(), "the first router ended before the second")
        self.fake.release()
        self.all_started([first], [ID])
        self.assertIn(f"router skip {ID} reason={STARTING}", self.said())


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
        """A live run holds its max_runs slot."""
        gate = os.path.join(self.root, "gate")
        self.scene([START, DONE], gate=gate, usage=usage(0.5))
        self.tick()
        [first] = self.runs()
        self.assertEqual((first["kind"], first["issue"]), ("start", ID))
        self.started()
        self.assertTrue(self.drive_running(ID))

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


class Recover(Parity):
    def setUp(self):
        super().setUp()
        self.seed(ID, "ENG: Session registry")

    def transcript(self, sid):
        cwd = session_cwd(os.path.join(self.workdirs, ID))
        project = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd))
        path = os.path.join(self.root, "claude-config", "projects", project, f"{sid}.jsonl")
        self.assertTrue(os.path.isfile(path), path)
        beside = os.walk(os.path.splitext(path)[0])
        return [path, *(os.path.join(top, name) for top, _, names in beside for name in names)]

    def interrupted(self):
        """Claimed by --issue: a tick's usage probe would hang with the agent."""
        self.scene([START], hang=True)
        res = self.router("--issue", ID)
        self.assertEqual(res.returncode, 0, res.stderr)
        [row] = self.runs()
        sid = row["sid"]
        live_tmux.wait(
            lambda: "start" in self.ledger(ID).get(sid, []), TIMEOUT, "write-back's start step"
        )
        [agent] = self.started()
        self.drive(ID).terminate()
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
        """A tick Recovers before it claims."""
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

    def test_a_logged_role_not_the_delegates_is_not_resumed_and_goes_to_in_review(self):
        """A changed delegate invalidates the saved role."""
        sid = self.interrupted()
        with self.fake.lock:
            self.fake.find(ID)["delegate"] = role_id("pm")
        self.scene([START, DONE], usage=usage(0.5))
        self.tick()
        self.assertEqual([(r["kind"], r["sid"]) for r in self.runs()], [("start", sid)])
        self.assertEqual(len(agent_calls(self.calls())), 1)
        why = "role (engineer) is not the delegate's (pm)"
        self.assertIn(f"router recover {ID} to=in_review reason={why}", self.routed())
        self.assertEqual(self.state(ID), "in_review")
        self.assertEqual(self.history(ID)[-1], ("in_progress", "in_review", HARNESS_USER))
        author, body = self.comments(ID)[-1]
        self.assertEqual(author, HARNESS_USER)
        self.assertTrue(body.startswith(f"The interrupted agent run's {why}; needs a look."), body)
        self.assertEqual(self.subscribers(ID), [HUMAN])


class Refusals(Parity):
    def assert_nothing_started(self):
        self.assertEqual((self.runs(), agent_calls(self.calls())), ([], []))
        sessions = self.server.tmux("list-sessions", "-F", "#{session_name}")
        self.assertEqual(sessions.stdout.split(), [MANAGER], sessions.stderr)

    def test_a_new_run_whose_target_check_fails_is_bounced_and_starts_no_agent_run(self):
        """`target.check` Invalid → bounce, as the role's app."""
        self.seed(ID, "ENG: Session registry")
        self.gh["repos"][REPO]["permissions"]["push"] = False
        self.write_gh()
        self.scene([START, DONE], usage=usage(0.5))
        self.tick()
        self.assert_nothing_started()
        self.assertEqual(self.state(ID), "in_review")
        self.assertEqual(
            self.history(ID),
            [("todo", "in_progress", HARNESS_USER), ("in_progress", "in_review", ENGINEER)],
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
        """v1 writeback.bounce of an issue promoted from a Handoff source."""
        now, pm = datetime.now(UTC), role_id("pm")
        self.fake.issue(
            "PRD: Session registry",
            identifier=SOURCE,
            state="in_progress",
            delegate=pm,
            assignee=self.ids[HUMAN],
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
        """v1 router_test.test_fr7_invalid_label_goes_to_review."""
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
                self.assertEqual(self.history(ident), [("todo", "in_review", HARNESS_USER)])
                [(author, body)] = self.comments(ident)
                self.assertEqual(author, HARNESS_USER)
                self.assertTrue(body.startswith(said), body)
                self.assertIn("then move the issue back to Todo.", body)
                self.assertEqual(self.subscribers(ident), [HUMAN])


if __name__ == "__main__":
    unittest.main()
