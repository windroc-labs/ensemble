"""router.py as a subprocess: it detaches the run's LinearRun, whose drive runs as a local process
and calls back ensemble-orchestrator writeback; fake_claude as client claude, FakeLinear over HTTP.
A claim runs to In Review; a crash resumes the same session; a Linear failure mid write-back fails
its callback, and Recover then writes the outcome back in process, without an agent; a Linear outage stops a tick and --issue; a tick's usage gate; a long description reaches the run; a
SIGTERM'd harness ends its run and its agent; the researcher's gate runs on a session's PYTHONPATH.
"""

import contextlib
import os
import shlex
import signal
import subprocess
import sys
import unittest

from ensemble.orchestrator.engine import config
from tests.delegate.replies import block, mark, option
from tests.orchestrator.case import PYTHONPATH
from tests.orchestrator.integration.flow_fixtures import (
    HUMAN,
    PROJECT,
    REPO,
    TIMEOUT,
    UPDRAFT,
    Flow,
    TmuxFlow,
    usage,
)
from tests.orchestrator.integration.segments import FIRST_WRITE, agent_calls, lookup
from tests.orchestrator.unit.board_ids import HARNESS_USER, role_id
from tests.session import live_tmux

ENGINEER = role_id("engineer")
ID, TITLE, BRANCH = "TASK-7", "ENG: Session registry", "TASK-7-session-registry"
PR = f"https://github.com/{REPO}/pull/1"
STARTED, READY = "Building the session registry.", "Added the session registry."
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
LEDGER = ["running:1", "start", "comment", f"attach:{PR}", "move:in_review", "outcome"]
ATTACHMENT = {"url": PR, "title": "Session registry"}
LONG = "\n".join(f"Requirement {n}: the registry keeps session {n} → its run." for n in range(400))
HARNESS_OPS = {
    "linear.Q_TEAM",
    "linear.Q_TASK_GROUP",
    "router.issues",
    "router.issues+relations",
    "router.Q_RECHECK",
    "linear.M_STATE",
    "issues.Q_ISSUE",
    "related.Q_SEARCH",
    "sessions.M_UPDATE",
    "linear.M_COMMENT_ID",
    "linear.Q_COMMENT",
}
ENGINEER_OPS = {
    "linear.Q_COMMENT",
    "linear.M_COMMENT_ID",
    "writeback.Q_ATTACHED",
    "linear.M_SUBSCRIBE",
    "writeback.M_ATTACH",
    "linear.Q_ISSUE_STATE",
    "linear.M_STATE",
}


# write-back looks each of the run's comments (start, outcome) up before posting it: its event may
# come again; not found is the fake's error
LOOKUPS = [lookup(ENGINEER)] * 2


HARNESS_MODULE = "-m ensemble.delegate.engine.harness"


def ancestor(pid):
    """pid's parent and its command line."""
    res = subprocess.run(
        ["ps", "-o", "ppid=", "-p", str(pid)],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=True,
    )
    parent = int(res.stdout)
    res = subprocess.run(
        ["ps", "-o", "command=", "-p", str(parent)],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=True,
    )
    return parent, res.stdout


def gone(pid):
    res = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=TIMEOUT
    )
    return res.stdout.strip() == "" or res.stdout.strip().startswith("Z")


class Router(TmuxFlow):
    def setUp(self):
        super().setUp()
        self.fake.issue(
            TITLE,
            identifier=ID,
            delegate=ENGINEER,
            assignee=self.ids[HUMAN],
            project=PROJECT,
            description="Add a session registry.",
        )

    def claim(self, mode="start", argv=("--issue", ID)):
        res = self.router(*argv)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.issue(ID)["assignee"], self.ids[HUMAN])
        last = self.runs()[-1]
        self.assertEqual(
            (last["kind"], last["issue"], last["role"], last["client"]),
            (mode, ID, "engineer", "claude"),
        )
        return last["sid"]

    def started(self, n=1):
        """The first n agent runs' fake_claude calls, once logged."""
        return live_tmux.wait(
            lambda: (calls := agent_calls(self.calls()))[n - 1 :] and calls,
            TIMEOUT,
            f"fake claude's start #{n}",
        )

    def input(self, sid):
        """run.jsonl's input record of sid's first run."""
        records = self.records(ID)
        at = next(i for i, r in enumerate(records) if r["kind"] == "session" and r["sid"] == sid)
        return next(r["text"] for r in records[at:] if r["kind"] == "input")

    def assert_built(self, sid, *, resumed=False):
        self.assertEqual(self.state(ID), "in_review")
        self.assertEqual(
            self.history(ID),
            [("todo", "in_progress", HARNESS_USER), ("in_progress", "in_review", ENGINEER)],
        )
        (author, session), *rest = self.comments(ID)
        self.assertEqual(author, HARNESS_USER)
        self.assertRegex(session, rf"^Run {sid} · done · .* · exit 0\n")
        self.assertEqual(
            rest,
            [
                (ENGINEER, f"Build started: {STARTED}"),
                (ENGINEER, f"Build ready:\nAgent report before delivery:\n\n> {READY}"),
            ],
        )
        self.assertEqual(self.attachments(ID), [ATTACHMENT])
        self.assertEqual(self.subscribers(ID), [HUMAN])
        # a resumed run's outcome is attempt 2's: its steps are scoped to it
        scoped = [s if s.startswith("attach:") else f"attempt:2:{s}" for s in LEDGER[2:]]
        ledger = [*LEDGER[:2], "running:2", *scoped] if resumed else LEDGER
        self.assertEqual(self.ledger(ID), {sid: ledger})
        [running] = [v["b"] for v in self.sent("linear.M_COMMENT_ID", HARNESS_USER)]
        self.assertTrue(running.startswith(f"Run {sid} · running · "), running)

    def test_claim_to_in_review(self):
        self.scene([START, DONE])
        sid = self.claim()
        self.wait_end(sid, ID)
        self.assert_built(sid)
        self.assertEqual(
            self.runs(),
            [
                {
                    "kind": "start",
                    "issue": ID,
                    "sid": sid,
                    "role": "engineer",
                    "client": "claude",
                    "delegate_kind": "harness",
                    "uuid": self.issue(ID)["id"],
                    "project": PROJECT["id"],
                    "target": REPO,
                    "task": None,
                }
            ],
        )
        lines = self.input(sid).splitlines()
        self.assertIn(f"Repo: {REPO}", lines)
        self.assertIn(f"Branch: {BRANCH}", lines)
        self.assertEqual(self.problems(), [FIRST_WRITE, *LOOKUPS])
        self.assertEqual(
            {(r.op, r.caller) for r in self.requests()},
            {(op, HARNESS_USER) for op in HARNESS_OPS} | {(op, ENGINEER) for op in ENGINEER_OPS},
        )
        self.assertTrue(
            self.sent("sessions.M_UPDATE", HARNESS_USER)[-1]["b"].startswith(f"Run {sid} · done · ")
        )

    def test_a_crash_mid_run_resumes_the_session(self):
        self.scene([START], hang=True)
        sid = self.claim()
        live_tmux.wait(
            lambda: "start" in self.ledger(ID).get(sid, []), TIMEOUT, "write-back's start step"
        )
        [agent] = self.started()
        pid, command = agent["pid"], ""
        for _ in range(4):  # the agent's nearest harness ancestor
            pid, command = ancestor(pid)
            if HARNESS_MODULE in command:
                break
        self.assertIn(f"{HARNESS_MODULE} {self.workdirs}/{ID}", command)
        os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            os.kill(agent["pid"], signal.SIGKILL)
        live_tmux.wait(lambda: not self.drive_running(ID), TIMEOUT, "its drive to end")
        self.assertEqual(self.state(ID), "in_progress")
        [(author, session)] = [c for c in self.comments(ID) if c[1].startswith(f"Run {sid} · ")]
        self.assertEqual(author, HARNESS_USER)
        self.assertTrue(session.startswith(f"Run {sid} · running · "), session)
        self.assertEqual(self.ledger(ID), {sid: ["running:1", "start"]})
        self.assertEqual([e for e in self.logged() if e["kind"] == "end"], [])

        self.scene([START, DONE])
        self.assertEqual(self.claim("resume"), sid)
        self.wait_end(sid, ID, ends=1)
        self.assertEqual(
            [(r["kind"], r["sid"]) for r in self.runs()], [("start", sid), ("resume", sid)]
        )
        first, second = self.started(2)
        self.assertEqual(option(first, "--session-id"), sid)
        self.assertEqual((option(second, "--resume"), option(second, "--session-id")), (sid, None))
        self.assert_built(sid, resumed=True)
        started = [
            v
            for v in self.sent("linear.M_COMMENT_ID", ENGINEER)
            if v["b"].startswith("Build started")
        ]
        self.assertEqual(len(started), 1)
        self.assertEqual(self.problems(), [FIRST_WRITE, *LOOKUPS])

    def test_a_linear_failure_mid_write_back_is_written_back_by_recover(self):
        self.fake.fail("linear.M_STATE", "503", caller=ENGINEER)
        self.scene([START, DONE])
        sid = self.claim()
        self.wait_end(sid, ID)
        self.assertEqual(self.state(ID), "in_progress")
        self.assertEqual(self.ledger(ID), {sid: LEDGER[:4]})
        self.assertEqual(
            self.comments(ID)[-1],
            (ENGINEER, f"Build ready:\nAgent report before delivery:\n\n> {READY}"),
        )
        self.assertEqual(self.attachments(ID), [ATTACHMENT])
        [error] = [e for e in self.logged() if e["kind"] == "step-error"]
        self.assertEqual(
            (error["src"], error["issue"], error["sid"], error["step"]),
            ("writeback", ID, sid, "move:in_review"),
        )
        self.assertIn("503", error["error"])
        self.assertEqual(
            [r["kind"] for r in self.records(ID) if r["kind"] == "callback_error"],
            ["callback_error"],
        )

        res = self.router("--issue", ID)
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)
        self.assertEqual([(r["kind"], r["sid"]) for r in self.runs()], [("start", sid)])
        self.assertEqual(len(agent_calls(self.calls())), 1)
        self.assertEqual(
            [e["kind"] for e in self.logged() if e["src"] == "router" and e.get("issue") == ID][-1],
            "recover",
        )
        self.assert_built(sid)
        ready = [
            v
            for v in self.sent("linear.M_COMMENT_ID", ENGINEER)
            if v["b"].startswith("Build ready")
        ]
        self.assertEqual(len(ready), 1)
        self.assertEqual(len(self.sent("writeback.M_ATTACH", ENGINEER)), 1)
        self.assertEqual(
            self.problems(), [FIRST_WRITE, *LOOKUPS, ("linear.M_STATE", ENGINEER, "503")]
        )

    def outage(self, *argv):
        self.fake.fail("linear.Q_TEAM", "503")
        res = self.router(*argv)
        err = "router.py: Linear unavailable: teams: HTTP 503\n" if "--issue" in argv else ""
        self.assertEqual((res.returncode, res.stdout, res.stderr), (1, "", err))
        self.assertEqual(
            [(r.op, r.caller, r.result) for r in self.requests()],
            [("linear.Q_TEAM", HARNESS_USER, "503")],
        )
        self.assertEqual(
            [{k: v for k, v in e.items() if k != "ts"} for e in self.logged()],
            [{"src": "router", "kind": "linear-error", "op": "teams", "status": 503}],
        )
        self.assertEqual(self.state(ID), "todo")

    def test_a_linear_outage_stops_the_tick(self):
        self.outage()

    def test_a_linear_outage_stops_issue_and_the_rerun_claims_it(self):
        self.outage("--issue", ID)
        self.scene([START, DONE])
        sid = self.claim()
        self.wait_end(sid, ID)
        self.assert_built(sid)
        self.assertEqual([e["kind"] for e in self.logged()].count("linear-error"), 1)

    def test_a_tick_at_95_percent_usage_skips_the_claim(self):
        self.scene([START, DONE], usage=usage(0.95))
        res = self.router()
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))
        self.assertEqual(
            self.said(),
            [f"router usage-skip {ID} client=claude reason=usage usage=five_hour=95% weekly=none"],
        )
        self.assertEqual((self.state(ID), self.runs()), ("todo", []))
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(agent_calls(self.calls()), [])
        self.assertEqual(self.sent("linear.M_STATE"), [])

    def test_a_tick_at_50_percent_usage_claims_and_finishes(self):
        self.scene([START, DONE], usage=usage(0.5))
        sid = self.claim("start", ())
        self.wait_end(sid, ID)
        self.assert_built(sid)
        self.assertEqual(
            [line for line in self.said() if line.startswith("router ")],
            [
                "router plan mode=new queue=1",
                f"router pick {ID} queue=1",
                f"router claim {ID} role=engineer client=claude client_source=default",
                f"router launch {ID} exit=0",
            ],
        )
        calls = self.calls()
        self.assertEqual([option(c, "--session-id") for c in agent_calls(calls)], [sid])
        self.assertEqual(len(calls), 2)  # and the usage probe

    def test_a_long_description_reaches_the_run_intact(self):
        with self.fake.lock:
            self.fake.find(ID)["description"] = LONG
        self.assertGreaterEqual(len(LONG.encode()), 20_000)
        self.scene([START, DONE])
        sid = self.claim()
        self.wait_end(sid, ID)
        self.assertIn(f"\n\n{LONG}\n\n", self.input(sid))
        self.assert_built(sid)

    def test_a_sigtermed_run_ends_its_run_and_its_agent(self):
        self.scene([START], hang=True)
        sid = self.claim()
        live_tmux.wait(
            lambda: "start" in self.ledger(ID).get(sid, []), TIMEOUT, "write-back's start step"
        )
        [agent] = self.started()
        self.drive(ID).terminate()
        self.wait_end(sid, ID)
        [end] = [e for e in self.logged() if (e["src"], e["kind"]) == ("writeback", "end")]
        self.assertEqual((end["issue"], end["exit"]), (ID, 128 + signal.SIGTERM))
        [delegate_end] = [record for record in self.records(ID) if record["kind"] == "end"]
        self.assertEqual((delegate_end["sid"], delegate_end["rc"]), (sid, 128 + signal.SIGTERM))
        live_tmux.wait(lambda: gone(agent["pid"]), TIMEOUT, f"fake claude {agent['pid']} to end")
        [(author, session)] = [c for c in self.comments(ID) if c[1].startswith(f"Run {sid} · ")]
        self.assertEqual(author, HARNESS_USER)
        self.assertRegex(session, rf"^Run {sid} · interrupted · .* · exit {128 + signal.SIGTERM}\n")
        self.assertEqual(
            (self.state(ID), self.ledger(ID)), ("in_progress", {sid: ["running:1", "start"]})
        )


class Gate(Flow):
    SHIMS = ("claude", "python3")

    def test_the_researchers_gate_on_a_sessions_core_only_pythonpath(self):
        shim = os.path.join(self.bin, "python3")
        with open(shim, "w") as f:
            f.write(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
        os.chmod(shim, 0o755)
        self.guard()
        self.scene([], usage=usage(0.5))
        res = subprocess.run(
            ["ensemble-orchestrator", "router", "--brake", "researcher"],
            cwd=self.root,
            env={
                **self.env(),
                "PYTHONPATH": PYTHONPATH,
                "PATH": os.pathsep.join((self.bin, config.path())),
            },
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
        self.assertNotIn("ModuleNotFoundError", res.stderr)
        self.assertEqual(
            (res.returncode, res.stdout), (0, "five_hour=50% weekly=none\n"), res.stderr
        )


if __name__ == "__main__":
    unittest.main()
