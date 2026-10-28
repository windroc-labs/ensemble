"""router.py --tui end to end: from MANAGER's pane an --issue claim or a tick prints the run's
Record; its drive and TUI live, written back as a headless run; a killed or closed run resumes
on the next --issue, `resume ID` at its display; no place for the TUI, a bad display or events
file: exit 2 before any Linear request.
"""

import functools
import os
import signal
import subprocess
import unittest

from ensemble.orchestrator.engine import config
from ensemble.utils import Record
from tests.delegate.replies import option
from tests.orchestrator.case import REPO
from tests.orchestrator.integration.flow_fixtures import (
    ENTRY,
    MANAGER,
    PROJECT,
    TIMEOUT,
    TmuxFlow,
)
from tests.orchestrator.integration.segments import (
    USAGE,
    Segment,
    agent_calls,
    lookup,
    tui_scene,
)
from tests.orchestrator.unit.board_ids import role_id
from tests.session import live_tmux

SESSION_CLI = os.path.join(REPO, "bin", "ensemble-session")
STAMP = r"[0-9]{2}:[0-9]{2}:[0-9]{2} "
ID = "TASK-7"


class Attended(Segment):
    def event_lines(self):
        lines = [line for line in live_tmux.events(self.events) if line]
        for line in lines:
            self.assertRegex(line, f"^{STAMP}")
        return [line[len("HH:MM:SS ") :] for line in lines]

    def here(self):
        res = subprocess.run(
            [SESSION_CLI, "ls", "--here"],
            cwd=self.root,
            env={**self.env(), **self.in_manager()},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        return [row.split("\t")[0] for row in res.stdout.splitlines()[1:]]

    def new_calls(self, seen):
        try:
            return agent_calls(self.calls())[seen:]
        except ValueError:  # a line half appended
            return None

    def test_an_issue_run_from_the_manager_is_attended_and_written_back_as_a_headless_one(self):
        ident = self.pm_ready()
        sid = self.claim(ident, "pm", self.agent_script("pm", "done", ident), tui=True)
        harness, tui = self.harness("pm", ident, sid), config.tui_name("pm", ident, sid)
        with self.step("attended: shown and listed"):
            self.assertEqual((self.drive_running(ident), self.live(tui)), (True, True))
            live_tmux.wait(
                functools.partial(live_tmux.assert_grid, self, self.server, MANAGER, [[tui]]),
                what=f"the grid [[{tui!r}]]",
            )
            self.assertNotIn(harness, self.here())
        self.agent_run()
        ended = (f"{harness} outcome done", f"{harness} exited 0")
        live_tmux.wait_events(
            self.events, [(harness, word) for word in ("outcome", "exited")], TIMEOUT
        )
        lines = self.event_lines()
        self.assertCountEqual([line for line in lines if line.startswith(f"{harness} ")], ended)
        self.assertEqual(set(lines) - set(ended), {f"{tui} done"})

    def test_a_tick_from_the_manager_starts_its_run_attended_and_an_idle_one_prints_nothing(self):
        ident = self.pm_ready()
        self.claim(ident, "pm", self.agent_script("pm", "done", ident), tui=True, tick=True)
        self.agent_run()
        with self.step("router: idle tick"):
            res = self.router("--tui", ENSEMBLE_EVENTS=self.events, **self.in_manager())
            self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)

    def drive_group(self, ident):
        """ident's harness process group, never this test's."""
        workdir = os.path.join(self.workdirs, ident)
        ps = subprocess.run(
            ["ps", "-A", "-ww", "-o", "pid=,pgid=,args="],
            capture_output=True,
            text=True,
            check=True,
        )
        rows = [line.split(None, 2) for line in ps.stdout.splitlines()]
        tail = f"-m ensemble.delegate.engine.harness {workdir}"
        [group] = [int(row[1]) for row in rows if row[2:] and row[2].endswith(tail)]
        self.assertNotEqual(group, os.getpgrp())
        return group

    def run_processes(self, ident):
        """Whether ident's drive or last write-back process lives on after its control ended."""
        workdir = os.path.join(self.workdirs, ident)
        marks = (
            f"ensemble.delegate.engine.harness {workdir}",
            f"--issue {ident} --workdir {workdir}",
        )
        ps = subprocess.run(
            ["ps", "-A", "-o", "command="], capture_output=True, text=True, check=True
        )
        return any(mark in c for c in ps.stdout.splitlines() for mark in marks)

    def interrupted(self, stop):
        """An attended run stopped by stop(ident, sid) after its start mark: its TUI ends, the
        issue stays In Progress, its session comment running or interrupted; --issue --tui resumes it.
        """
        ident, app = self.pm_ready(), role_id("pm")
        sid = self.claim(ident, "pm", self.agent_script("pm", "open", ident), tui=True)
        with self.step("agent: started, then waiting"):
            with open(self.pending.gate, "w"):
                pass
            live_tmux.wait(
                lambda: "start" in self.ledger(ident).get(sid, []), TIMEOUT, "the start step"
            )
            self.expect(ident, comments=[(app, "start")], ledger={sid: ["running:1", "start"]})
            self.expect_problem(lookup(app))
        with self.step("harness stopped"):
            stop(ident, sid)
            tui = config.tui_name("pm", ident, sid)
            live_tmux.wait(lambda: not self.drive_running(ident), TIMEOUT, "its drive ended")
            live_tmux.wait(lambda: not self.live(tui), TIMEOUT, f"{tui} gone")
            live_tmux.wait(lambda: not self.run_processes(ident), TIMEOUT, "its processes ended")
            self.pending = None
            self.assertEqual(self.state(ident), "in_progress")
            status = self.session_status(ident, sid)
            self.assertIn(status, ("running", "interrupted"))
            self.expect_session(ident, sid, status)
        script = self.agent_script("pm", "done", ident)
        self.resume(ident, "pm", sid, script, tui=True, after_stop=True)
        self.agent_run()

    def test_killing_its_drive_and_tui_interrupts_the_run_and_the_next_router_run_resumes(self):
        def kill(ident, sid):
            # the TUI outlives the drive's SIGKILL; its kill interrupts the attempt
            os.killpg(self.drive_group(ident), signal.SIGKILL)
            tui = config.tui_name("pm", ident, sid)
            live_tmux.crash_owner(self.ensemble, tui)
            res = self.server.tmux("kill-session", "-t", f"={tui}")
            self.assertEqual(res.returncode, 0, res.stderr)

        self.interrupted(kill)

    def test_close_ends_the_harness_and_the_tui_and_the_next_router_run_resumes(self):
        def close(ident, _sid):
            res = self.script(ENTRY, "close", ident)
            self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))

        self.interrupted(close)

    def test_resume_brings_a_closed_tui_back_attended_at_its_display_its_record_unchanged(self):
        ident = self.pm_ready()
        sid = self.claim(ident, "pm", self.agent_script("pm", "none", ident), tui=True)
        record, tui = self.printed, config.tui_name("pm", ident, sid)
        self.agent_run()
        self.human_move(ident, "in_progress", "human: retry failed run")
        live_tmux.crash_owner(self.ensemble, tui)  # its display alone ends no worker
        self.assertEqual(self.server.tmux("kill-session", "-t", f"={tui}").returncode, 0)
        self.scene(**tui_scene(self.agent_script("pm", "done", ident)), usage=USAGE)
        seen = len(agent_calls(self.calls()))
        res = self.script(ENTRY, "resume", ident)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(Record.parse(res.stdout), record)
        [call] = live_tmux.wait(lambda: self.new_calls(seen), TIMEOUT, "the resumed agent")
        self.assertEqual((option(call, "--resume"), option(call, "--output-format")), (sid, None))
        self.wait_end(sid, ident, ends=2, role="pm")
        self.assertTrue(self.live(tui))
        self.assertEqual(
            (self.state(ident), self.ledger(ident)[sid][-1]), ("in_review", "attempt:2:outcome")
        )


class Refused(TmuxFlow):
    def setUp(self):
        super().setUp()
        self.fake.issue(
            "PRD: Session registry",
            identifier=ID,
            delegate=role_id("pm"),
            project=PROJECT,
        )
        self.events = os.path.join(self.root, "events")

    def test_no_place_for_the_tui_a_bad_display_or_events_file_exit_2_before_linear(self):
        missing = os.path.join(self.root, "missing", "events")
        inside, events = self.in_manager(), {"ENSEMBLE_EVENTS": self.events}
        cases = (
            ("outside tmux", (), events, "no pane to show the TUI beside ("),
            (
                "a malformed display",
                ("--display", f"{MANAGER}/diagonal"),
                {**inside, **events},
                f"display '{MANAGER}/diagonal': not ",
            ),
            (
                "a missing split_from",
                ("--display", "//nosuch"),
                {**inside, **events},
                "no tmux session nosuch",
            ),
            (
                "an unwritable events file",
                ("--events", missing),
                inside,
                f"events file {missing}: No such file or directory",
            ),
            ("no events file", (), inside, "--tui needs --events FILE or $ENSEMBLE_EVENTS"),
        )
        for n, (name, extra, env, said) in enumerate(cases):
            mode = ("--issue", ID) if n % 2 else ()
            with self.subTest(name, mode=mode):
                res = self.router(*mode, "--tui", *extra, **env)
                self.assertEqual((res.returncode, res.stdout), (2, ""), res.stderr)
                self.assertTrue(res.stderr.startswith(f"router.py: {said}"), res.stderr)
                self.assertEqual(len(res.stderr.splitlines()), 1, res.stderr)
        self.assertEqual(self.requests(), [])
        listed = self.server.tmux("list-sessions", "-F", "#{session_name}")
        self.assertEqual(listed.stdout.split(), [MANAGER], listed.stderr)
        self.assertEqual(
            (self.runs(), self.state(ID), os.path.exists(self.events)), ([], "todo", False)
        )


if __name__ == "__main__":
    unittest.main()
