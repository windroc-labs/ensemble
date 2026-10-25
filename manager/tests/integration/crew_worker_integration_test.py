"""Workers through `ensemble-crew run -- ensemble-session start` (FR-17, FR-20) on the real
commands (manager/tests/live_crew.py): the worker's Record and entry, a gone worker's crew resume,
a live one's no-op, main's crew_integration_test.py dialog waits (C:429, C:528) on this entry,
main's test_client_flag_starts_its_worker_and_a_restart_waits_for_the_old_one (FR-36), one
pane per live worker however often crew attach shows it, and a resume's /color changing neither its
state nor the events file.
"""

import glob
import json
import os
import shlex
import sys
import time

from ensemble.manager import State

from manager.tests.live_crew import MANAGER, LiveCrew, has_pair, logged
from session.tests import live_tmux
from session.tests.integration.clients.claude import fake_claude
from session.tests.integration.clients.codex import fake_tui

PERMISSION = {"kind": "hook", "event": "PermissionRequest", "tool_name": "Edit"}
EXTRA = '[session.clients.ext]\nimplementation = "ensemble.session.clients.claude:ClaudeClient"\n'
# A client ignoring SIGHUP and SIGTERM: logs its start (and any earlier start still running), runs
# the fake claude with both restored, outlives it by LINGER s, logs its exit.
LINGERING = """import json, os, signal, subprocess, sys, time
LINGER = 1
log, fake = sys.argv[1], sys.argv[2]
for each in (signal.SIGHUP, signal.SIGTERM):
    signal.signal(each, signal.SIG_IGN)
def running(pid):
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                          env={"PATH": os.defpath, "LC_ALL": "C"}).stdout.strip()
    return bool(stat) and not stat.startswith("Z")
def note(what, **more):
    with open(log, "a") as f:
        entry = {"what": what, "pid": os.getpid(), "argv": sys.argv[3:], "cwd": os.getcwd()}
        f.write(json.dumps({**entry, **more}) + "\\n")
try:
    with open(log) as f:
        before = [json.loads(line)["pid"] for line in f]
except FileNotFoundError:
    before = []
note("start", running=sorted(p for p in set(before) if running(p)))
def restored():
    for each in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(each, signal.SIG_DFL)
subprocess.call([sys.executable, "-I", fake, *sys.argv[3:]], preexec_fn=restored)
time.sleep(LINGER)
note("exit")
"""


class WorkerTest(LiveCrew):
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
        resumed = self.record(self.crew("resume", "w1", **self.scene("w1")))
        self.assertEqual((resumed.name, resumed.sid, resumed.display), ("w1", record.sid, MANAGER))
        self.assertTrue(self.live("w1"))
        self.assertTrue(has_pair(logged(self.log("w1"))[-1]["argv"], "--resume", record.sid))
        self.assertEqual(self.roster()["w1"].state, State.LIVE)


class ColorTest(LiveCrew):
    def state(self, name: str) -> str:
        (status,) = map(json.loads, self.ok(self.crew("status", name)).stdout.splitlines())
        return status["state"]

    def test_resume_color_keeps_state_and_events(self):
        self.record(self.worker("w1"))
        self.wait_event("w1", "done")
        self.kill("w1")
        self.ok(self.crew("attach"))
        before = live_tmux.events(self.events)
        self.record(self.crew("resume", "w1", **self.scene("w1")))
        state = self.state("w1")
        pid = logged(self.log("w1"))[-1]["pid"]
        self.until(
            lambda: (
                [c["line"] for c in logged(self.log("w1", "commands")) if c["pid"] == pid]
                == ["/color"]
            ),
            "the resume's /color",
        )
        time.sleep(1)
        self.assertEqual((state, self.state("w1")), ("working", "working"))
        self.assertEqual(live_tmux.events(self.events), before)


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

    def test_the_codex_trust_dialog_at_start_is_said_on_crew_runs_stderr(self):
        fake_tui.install(self.server.bin)
        never, scenario = (os.path.join(self.root, n) for n in ("never", "w1.codex.json"))
        trust = "Trust this folder? Codex can read, edit, and run files here.\n\n› 1. Trust and continue"
        steps = [{"kind": "screen", "text": trust}, {"kind": "pause", "file": never}]
        with open(scenario, "w", encoding="utf-8") as f:
            json.dump({"tui_turns": [steps]}, f)
        codex = {"FAKE_SCENARIO": scenario, "FAKE_LOG": os.path.join(self.root, "w1.codex.jsonl")}
        res = self.worker("w1", client="codex", **codex)
        self.assertEqual(self.record(res).detail, "codex")
        self.assertIn("session w1: waits on a dialog: Trust this folder?", res.stderr.splitlines())
        self.wait_event("w1", "blocked")
        self.assertEqual(self.server.option("w1", "@state"), "blocked")
        self.assertIn("Trust this folder?", self.ok(self.session("read", "w1")).stdout)


class RestartTest(LiveCrew):
    def test_client_flag_starts_its_worker_and_a_restart_waits_for_the_old_one(self):
        with open(os.path.join(self.ensemble, "config.local.toml"), "w", encoding="utf-8") as f:
            f.write(EXTRA)
        script, log = (os.path.join(self.root, n) for n in ("lingering.py", "lingering.jsonl"))
        with open(script, "w", encoding="utf-8") as f:
            f.write(LINGERING)
        os.makedirs(self.spies)
        q = shlex.quote
        with open(os.path.join(self.spies, "claude"), "w", encoding="utf-8") as f:
            f.write(
                f"#!/bin/sh\nexec {q(sys.executable)} -I {q(script)} {q(log)} "
                f'{q(fake_claude.__file__)} "$@"\n'
            )
        os.chmod(os.path.join(self.spies, "claude"), 0o755)
        record = self.record(self.worker("w1", client="ext"))
        self.assertEqual((record.detail, self.roster()["w1"].record), ("ext", record))
        self.wait_event("w1", "done")
        (begun,) = logged(log)
        self.assertEqual((begun["what"], begun["cwd"]), ("start", self.cwd))
        self.assertTrue(has_pair(begun["argv"], "--session-id", record.sid), begun["argv"])
        restarted = self.record(self.session("restart", "w1", **self.scene("w1")))
        self.assertEqual((restarted.sid, restarted.detail), (record.sid, "ext"))
        _, ended, resumed = self.until(
            lambda: len(logged(log)) == 3 and logged(log), "the new start"
        )
        self.assertEqual((ended["what"], ended["pid"]), ("exit", begun["pid"]))
        self.assertEqual((resumed["what"], resumed["running"]), ("start", []))
        self.assertTrue(has_pair(resumed["argv"], "--resume", record.sid), resumed["argv"])
