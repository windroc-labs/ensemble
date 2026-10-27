"""router.py --tui end to end (Segment): run from MANAGER's pane with $ENSEMBLE_EVENTS, an --issue
claim or a tick prints the run's one pipeline Record and its sessions and attach command
(FR-28); its harness and TUI live, the TUI in MANAGER's grid, the harness listed by MANAGER's
`ensemble-session ls --here`; written back as a headless run is; the events file gets the TUI's
turn ends, one outcome and the harness's exit; an idle tick prints nothing. Killing the harness and
TUI sessions, or `ensemble-orchestrator close ID`, ends the run and leaves the issue In Progress for the
next router.py --issue to resume. `ensemble-orchestrator resume ID` brings a closed TUI's run back attended
at its display, its Record unchanged. No place for the TUI, a bad display or events file, or none:
exit 2 before any Linear request, starting nothing.
"""

import functools
import os
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
        """The events file's lines without their `HH:MM:SS ` stamp."""
        lines = [line for line in live_tmux.events(self.events) if line]
        for line in lines:
            self.assertRegex(line, f"^{STAMP}")
        return [line[len("HH:MM:SS ") :] for line in lines]

    def here(self):
        """The session names MANAGER's `ensemble-session ls --here` lists."""
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
            self.assertEqual((self.live(harness), self.live(tui)), (True, True))
            live_tmux.wait(
                functools.partial(live_tmux.assert_grid, self, self.server, MANAGER, [[tui]]),
                what=f"the grid [[{tui!r}]]",
            )
            self.assertIn(harness, self.here())
        self.agent_run()
        ended = (f"{harness} outcome done", f"{harness} exited 0")
        live_tmux.wait(lambda: ended[1] in self.event_lines(), TIMEOUT, f"`{ended[1]}`")
        lines = self.event_lines()
        self.assertEqual([line for line in lines if line.startswith(f"{harness} ")], list(ended))
        self.assertEqual(set(lines) - set(ended), {f"{tui} done"})

    def test_a_tick_from_the_manager_starts_its_run_attended_and_an_idle_one_prints_nothing(self):
        ident = self.pm_ready()
        self.claim(ident, "pm", self.agent_script("pm", "done", ident), tui=True, tick=True)
        self.agent_run()
        with self.step("router: idle tick"):
            res = self.router("--tui", ENSEMBLE_EVENTS=self.events, **self.in_manager())
            self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)

    def run_processes(self, ident):
        """Whether ident's run still has a harness or write-back process: the session list can drop
        a stopped harness before its process ends, and its last callback, in its own process group,
        outlives it.
        """
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
        """An attended run waiting on after its start mark, stopped by stop(ident, sid): its TUI
        ends and the issue stays In Progress; router.py --issue --tui resumes it to done.
        The stopped harness's last write-back may or may not have run: its session comment says
        running or interrupted, and no write-back end of the session is counted after the stop.
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
            harness, tui = self.harness("pm", ident, sid), config.tui_name("pm", ident, sid)
            for name in (harness, tui):
                live_tmux.wait(lambda name=name: not self.live(name), TIMEOUT, f"{name} gone")
            live_tmux.wait(lambda: not self.run_processes(ident), TIMEOUT, "its processes ended")
            self.pending = None
            self.assertEqual(self.state(ident), "in_progress")
            status = self.session_status(ident, sid)
            self.assertIn(status, ("running", "interrupted"))
            self.expect_session(ident, sid, status)
        script = self.agent_script("pm", "done", ident)
        self.resume(ident, "pm", sid, script, tui=True, after_stop=True)
        self.agent_run()

    def test_killing_its_sessions_interrupts_the_run_and_the_next_router_run_resumes(self):
        def kill(ident, sid):
            # the harness's kill leaves its owned TUI: the TUI's kill interrupts the attempt
            for name in (self.harness("pm", ident, sid), config.tui_name("pm", ident, sid)):
                res = self.server.tmux("kill-session", "-t", f"={name}")
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
        self.assertEqual((self.state(ident), self.ledger(ident)[sid][-1]), ("in_review", "outcome"))


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
        for name, extra, env, said in cases:
            for mode in (("--issue", ID), ()):
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
