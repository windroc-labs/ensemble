"""Role runs through `ensemble-crew run -- ensemble-delegate run … --detach` (FR-18, FR-22, FR-35)
on the real commands (manager/tests/live_crew.py). Ports main's test_detach_events_default,
test_killed_role_run_gone_then_recovered, test_close_a_role_run_by_its_tui_session and
test_a_role_run_ended_on_needs_input_is_waiting_and_left_alone_by_attach_resume.
"""

import json
import os

from ensemble.manager import State
from ensemble.utils import Record

from delegate.tests.integration.cli import ASKING, DONE
from delegate.tests.replies import block, mark
from manager.tests.live_crew import MANAGER, ROLE, LiveCrew, has_pair, logged
from session.tests import live_tmux

START = mark("start", "echoing")


class RoleCase(LiveCrew):
    def started(self, name: str, *, mode: str = "tui", **kw) -> Record:
        """crew run `name` -- a role run; its Record, checked, and its entry live."""
        record = self.record(self.role(name, mode=mode, **kw))
        tui = f"{ROLE}-{record.sid[:8]}"
        work = self.workdir(name)
        self.assertEqual(
            (record.kind, record.name, record.sessions, record.workdir, record.ctl, record.resume),
            (
                "role",
                tui,
                (f"{tui}-drive", tui) if mode == "tui" else (f"{tui}-drive",),
                work,
                ("ensemble-delegate",),
                ("ensemble-delegate", "resume", work),
            ),
        )
        entry = self.roster()[name]
        self.assertEqual((entry.state, entry.record, entry.sid), (State.LIVE, record, record.sid))
        return record


class RoleTest(RoleCase):
    def test_detach_events_default(self):
        record = self.started("r", steps=[START], turns=[[block(DONE)]])
        harness, tui = record.sessions
        self.wait_event(harness, "exited")
        self.wait_event(tui, "done", 2)
        self.assertEqual(self.words(tui), [["done"], ["done"]])
        self.assertEqual(self.words(harness), [["outcome", "done"], ["exited", "0"]])
        self.ok(self.crew("attach"))
        self.assertEqual(self.roster()["r"].state, State.DONE)

    def test_killed_role_run_gone_then_recovered(self):
        record = self.started("r", steps=[START])
        harness, tui = record.sessions
        self.killed("r", record)
        turns = self.words(tui).count(["done"])
        resumed = self.record(self.crew("resume", "r", **self.scene("r", [START])))
        self.assertEqual(
            (resumed.sid, resumed.sessions, resumed.display),
            (record.sid, record.sessions, record.display),
        )
        self.assertTrue(record.display.startswith(MANAGER), record.display)
        self.until(lambda: self.live(harness, tui), "the harness and the tui back")
        self.until(
            lambda: live_tmux.assert_grid(self, self.server, MANAGER, [[tui]]),
            f"the grid [[{tui}]]",
        )
        self.assertTrue(has_pair(logged(self.log("r"))[-1]["argv"], "--resume", record.sid))
        self.wait_event(tui, "done", turns + 1)
        entries = self.roster()
        self.assertEqual(list(entries), ["r"])
        self.assertEqual((entries["r"].state, entries["r"].record), (State.LIVE, resumed))

    def test_a_gone_role_run_resumed_by_hand_is_live_at_the_next_attach(self):
        record = self.started("r", steps=[START])
        self.killed("r", record)
        work = self.workdir("r")
        hand = self.record(
            self.call("ensemble-delegate", "resume", work, **self.scene("r", [START]))
        )
        self.assertEqual(hand.sid, record.sid)
        self.ok(self.crew("attach"))
        entry = self.roster()["r"]
        self.assertEqual((entry.state, entry.record), (State.LIVE, record))

    def test_close_a_role_run_by_its_tui_session(self):
        self.record(self.worker("w1"))
        record = self.started("r", steps=[START])
        harness, tui = record.sessions
        calls = {
            command: self.spy(command) for command in ("ensemble-session", "ensemble-delegate")
        }
        self.ok(self.crew("attach"))
        for log in calls.values():
            with open(log, encoding="utf-8") as f:
                self.assertEqual(
                    [c for c in f.read().splitlines() if c.startswith("status")], ["status"]
                )
        self.assertEqual(self.ok(self.crew("close", tui)).stdout, "")
        self.until(lambda: self.gone(harness, tui), f"{harness} and {tui} gone")
        self.assertEqual(list(self.roster()), ["w1"])
        self.assertTrue(self.live("w1"))

    def test_a_closed_run_reopens_on_its_own_sid(self):
        board = os.path.join(self.root, "board")
        work = os.path.join(board, "work", "TASK-9")
        os.makedirs(work)
        run = ("ensemble-delegate", "run", "--role", ROLE, "--input", "Hello.", "--workdir", work)
        argv = (*run, "--out", os.path.join(work, "out.md"), "--mode", "tui", "--detach")
        record = self.record(self.launch("TASK-9", argv, steps=[START], turns=[[block(DONE)]]))
        harness, tui = record.sessions
        self.wait_event(harness, "exited")
        self.ok(self.crew("close", "TASK-9"))
        self.until(lambda: self.gone(harness, tui), f"{harness} and {tui} gone")
        self.assertEqual(self.roster(), {})
        reopen = ("reopen", "--work-dir", board, "TASK-9", "--display", "none")
        reopened = self.record(self.crew(*reopen, **self.scene("TASK-9")))
        self.assertEqual((reopened.name, reopened.sid), (tui, record.sid))
        self.until(lambda: self.live(tui), f"{tui} back")
        self.assertTrue(has_pair(logged(self.log("TASK-9"))[-1]["argv"], "--resume", record.sid))
        entry = self.roster()[tui]
        self.assertEqual((entry.state, entry.sid, entry.record), (State.LIVE, record.sid, reopened))

    def test_a_role_run_ended_on_needs_input_is_waiting_and_left_alone(self):
        record = self.started("r", steps=[block(ASKING)], mode="headless")
        (harness,) = record.sessions
        self.wait_event(harness, "exited")
        for _ in range(2):
            self.ok(self.crew("attach"))
            self.assertEqual(self.roster()["r"].state, State.WAITING)
        outcome = json.loads(
            self.ok(self.call("ensemble-delegate", "outcome", self.workdir("r"))).stdout
        )
        self.assertEqual(outcome["questions"], ASKING["questions"])
        starts = len(logged(self.log("r")))
        res = self.crew("resume", "r")
        self.assertEqual((res.returncode, res.stdout), (1, ""), res.stderr)
        self.assertIn("waiting: give --input to answer", res.stderr)
        self.assertEqual(len(logged(self.log("r"))), starts)
        self.assertTrue(self.gone(harness))
        self.assertEqual(self.roster()["r"].state, State.WAITING)
