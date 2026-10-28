"""Workers through `ensemble-crew run -- ensemble-session start` (FR-17, FR-20) on the real
commands (tests/manager/live_crew.py): the worker's Record and entry, a gone worker's crew resume
whose /color changes neither its state nor the events file, a live one's no-op, a start's
permission dialog on crew run's stderr, and one pane per live worker however often crew attach
shows it.
"""

import glob
import json
import os

from ensemble.manager import State
from tests.manager.live_crew import MANAGER, LiveCrew, has_pair, logged
from tests.session import live_tmux

PERMISSION = {"kind": "hook", "event": "PermissionRequest", "tool_name": "Edit"}


class WorkerTest(LiveCrew):
    def state(self, name: str) -> str:
        (status,) = map(json.loads, self.ok(self.crew("status", name)).stdout.splitlines())
        return status["state"]

    def test_a_worker_runs_through_crew_run_and_a_gone_one_comes_back_by_crew_resume(self):
        record = self.record(self.worker("w1", note="first"))
        self.assertEqual(
            (record.kind, record.name, record.sessions, record.ctl, record.resume),
            ("worker", "w1", ("w1",), ("ensemble-session",), ("ensemble-session", "resume", "w1")),
        )
        self.assertEqual(record.display, MANAGER)
        entry = self.roster()["w1"]
        self.assertEqual(
            (entry.state, entry.record, entry.note, entry.sid),
            (State.LIVE, record, "first", record.sid),
        )
        (begun,) = logged(self.log("w1"))
        self.assertEqual(begun["cwd"], self.cwd)
        self.assertEqual(begun["argv"][-1], "hi")
        self.assertTrue(has_pair(begun["argv"], "--session-id", record.sid), begun["argv"])
        self.wait_event("w1", "done")
        self.assertEqual(
            glob.glob(os.path.join(self.ensemble, "managers", "**", "worker.json")), []
        )
        self.assertTrue(
            os.path.isfile(os.path.join(self.ensemble, "workers", record.sid, "worker.json"))
        )

        self.assertEqual(self.ok(self.crew("resume", "w1")).stdout, record.to_line() + "\n")
        self.assertEqual(len(logged(self.log("w1"))), 1)

        self.kill("w1")
        out = self.ok(self.crew("attach")).stdout.splitlines()
        self.assertIn("resume: ensemble-crew resume --manager mgr w1", out)
        self.assertEqual(self.roster()["w1"].state, State.GONE)
        self.assertTrue(self.gone("w1"))
        before = live_tmux.events(self.events)
        resumed = self.record(self.crew("resume", "w1", **self.scene("w1")))
        self.assertEqual((resumed.name, resumed.sid, resumed.display), ("w1", record.sid, MANAGER))
        state = self.state("w1")
        self.assertTrue(self.live("w1"))
        begun = logged(self.log("w1"))[-1]
        self.assertTrue(has_pair(begun["argv"], "--resume", record.sid))
        self.assertEqual(self.roster()["w1"].state, State.LIVE)
        self.until(
            lambda: (
                [c["line"] for c in logged(self.log("w1", "commands")) if c["pid"] == begun["pid"]]
                == ["/color"]
            ),
            "the resume's /color",
        )
        self.assertEqual((state, self.state("w1")), ("working", "working"))
        self.ok(self.session("send", "w1", "hi"))
        lines = self.wait_event("w1", "done", 2)
        self.assertEqual(lines[: len(before)], before)
        self.assertEqual([line.split(" ")[1:] for line in lines[len(before) :]], [["w1", "done"]])


class PaneTest(LiveCrew):
    def test_three_attaches_leave_each_worker_its_one_pane(self):
        for name in ("w1", "w2"):
            self.record(self.worker(name))
        window = f"={MANAGER}:"
        panes = {name: self.server.pane_of(name) for name in ("w1", "w2")}
        before = set(self.server.panes(window))
        self.assertTrue(set(panes.values()) <= before, (panes, before))
        for _ in range(3):
            self.ok(self.crew("attach"))
            self.assertEqual(set(self.server.panes(window)), before)
            self.assertEqual({name: self.server.pane_of(name) for name in panes}, panes)


class DialogTest(LiveCrew):
    def test_a_permission_hook_at_start_is_said_on_crew_runs_stderr(self):
        steps = [PERMISSION, {"kind": "dialog", "text": "Allow this edit?"}, "Edited."]
        res = self.worker("w1", steps=steps)
        self.assertEqual(self.record(res).name, "w1")
        self.assertIn("session w1: waits on a dialog: Edit", res.stderr.splitlines())
        blocked = [line for line in self.wait_event("w1", "blocked") if " w1 blocked" in line]
        self.assertEqual(
            self.ok(self.crew("next-event", "--after", "0", "--gen", "0")).stdout,
            f"1 {blocked[0]}\n",
        )
        self.assertIn("Allow this edit?", self.ok(self.session("read", "w1")).stdout)
