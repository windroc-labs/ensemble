"""delegate/cli.py run --detach as a subprocess on a private tmux server (session/tests/live_tmux.py),
a control-mode client on the manager, the session config's ClaudeClient running
session/tests/integration/clients/claude/fake_claude.py: the one Record line whose sessions are live
on return, the harness session, the TUI beside the caller or in no pane, the harness's outcome and
exited lines, a kill-session of the harness session, an events path tmux would misread, the events
file and sid a launcher gives in $ENSEMBLE_EVENTS and $ENSEMBLE_SID, a needs_input run answered by
`resume WORKDIR --input TEXT` (refused while the run lives) whose outcome a
`resume WORKDIR` then leaves alone. Ports main's
tests/integration/core/drive_tui_integration_test.py
test_detach_from_the_manager. Its test_detach_events_default and main's
manager/tests/integration/crew_integration_test.py role-run cases run through ensemble-crew in
manager/tests/integration/crew_*_integration_test.py.
"""

import contextlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import unittest

from ensemble.delegate import DelegateEvent
from ensemble.delegate.engine.types import Layout
from ensemble.session import SIGNALS
from ensemble.utils import Record

from delegate.tests.case import PY, PYTHONPATH, Case
from delegate.tests.fakes import callback_recorder
from delegate.tests.integration.cli import ASKING, DONE, RUN, SID
from delegate.tests.replies import block, mark, running
from session.tests import live_tmux
from session.tests.integration.clients.claude import fake_claude

MANAGER = "mgr"
ROLE = "dummy-tester"
TUI = f"{ROLE}-{SID[:8]}"
HARNESS = f"{TUI}-drive"
STAMP = "[0-9]{2}:[0-9]{2}:[0-9]{2}"
TURN_END = re.compile(rf"{STAMP} {TUI} done")
HARNESS_LINE = re.compile(rf"{STAMP} {HARNESS} ((?:outcome [a-z_]+)|(?:exited -?[0-9]+))")
START = mark("start", "echoing")
WAIT = 60  # seconds: a run
ANSWER = "Use option B."
SECRET = "sk-fake-441"
RESUME = (*RUN[:-1], "resume")


class Detach(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.server = live_tmux.Server(self, self.home)
        self.server.start(MANAGER)
        self.server.attach(MANAGER)
        self.proj, self.work = (os.path.join(self.dir, d) for d in ("proj", "work"))
        for d in (self.proj, self.work):
            os.mkdir(d)
        self.out = os.path.join(self.work, "out.md")
        self.events, self.scenario = (
            os.path.join(self.dir, f) for f in ("events", "scenario.json")
        )

    def detach(
        self,
        *extra: str,
        steps,
        turns=(),
        mode="headless",
        inside=None,
        events=None,
        launcher=False,
        text="Hello.",
        **scenario,
    ):
        """launcher: the events file and sid come as $ENSEMBLE_EVENTS and $ENSEMBLE_SID, as
        ensemble-crew run gives them.
        """
        with open(self.scenario, "w") as f:
            json.dump({"steps": steps, "turns": list(turns), **scenario}, f)
        self.events_used = events or self.events
        given = {"ENSEMBLE_EVENTS": self.events_used, "ENSEMBLE_SID": SID}
        argv = [*RUN, "--role", ROLE, "--input", text, "--out", self.out, "--workdir"]
        argv += [self.work, "--mode", mode, "--detach", *extra]
        argv += [] if launcher else ["--sid", SID, "--events", self.events_used]
        return self.cli(argv, inside=inside, given=given if launcher else {})

    def cli(self, argv, *, inside=None, given=None) -> subprocess.CompletedProcess:
        env = self.server.env(
            PYTHONPATH=PYTHONPATH,
            CLAUDE_CONFIG_DIR=os.path.join(self.dir, "config"),
            **{fake_claude.ENV: self.scenario},
            **(self.server.inside(inside) if inside else {}),
            **(given or {}),
        )
        return subprocess.run(
            argv,
            cwd=self.proj,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=WAIT,
        )

    def said(self, res: subprocess.CompletedProcess, *, tui: bool) -> Record:
        """The one Record line, every session it lists live when the command returned."""
        live = {name: self.live(name) for name in (HARNESS, TUI)}
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(res.stdout.splitlines()), 1, res.stdout)
        record = Record.parse(res.stdout)
        sessions = (HARNESS, TUI) if tui else (HARNESS,)
        self.assertEqual(
            (record.kind, record.name, record.sessions, record.sid, record.workdir),
            ("role", TUI, sessions, SID, self.work),
        )
        self.assertEqual(
            (record.ctl, record.resume),
            (("ensemble-delegate",), ("ensemble-delegate", "resume", self.work)),
        )
        self.assertTrue(all(live[name] for name in sessions), live)
        self.assertIn(f"delegate: session {SID}\n", res.stderr)
        return record

    def poke(self) -> None:
        # tmux on Linux (CI: 3.4) can leave an exited pane's process unreaped until another child of
        # the server exits
        self.server.tmux("run-shell", "-b", "true")

    def live(self, name: str) -> bool:
        return self.server.tmux("has-session", "-t", f"={name}").returncode == 0

    def until(self, check, what: str):
        def poked():
            self.poke()
            return check()

        return live_tmux.wait(poked, WAIT, what=what)

    def ended(self, turn_ends: int = 0) -> list[str]:
        """The events file's lines, shaped, once they hold the harness's exited line and turn_ends
        turn ends and the harness session is gone.
        """

        def check():
            shaped = shape(live_tmux.events(self.events_used))
            return (
                any(s.startswith("exited ") for s in shaped)
                and shaped.count("turn end") >= turn_ends
            )

        self.until(check, f"`{HARNESS} exited` and {turn_ends} turn ends")
        self.until(lambda: not self.live(HARNESS), f"{HARNESS} gone")
        return shape(live_tmux.events(self.events_used))

    def harness_lines(self, shaped: list[str]) -> list[str]:
        return [line for line in shaped if line != "turn end"]

    def records(self):
        try:
            with open(os.path.join(Layout(self.work).run, "run.jsonl")) as stream:
                return [json.loads(line) for line in stream]
        except FileNotFoundError:
            return []

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.records()]

    def last(self, kind: str) -> dict:
        return [e for e in self.records() if e["kind"] == kind][-1]

    def read(self, path: str) -> str:
        with open(path) as f:
            return f.read()

    def on_event(self, *rules: str) -> tuple[str, str]:
        """`--on-event` and a recorder command logging to self.callbacks."""
        self.callbacks = os.path.join(self.dir, "callbacks.log")
        argv = [sys.executable, "-P", "-S", callback_recorder.__file__, self.callbacks, *rules]
        return "--on-event", shlex.join(argv)

    def delivered(self) -> list[dict]:
        try:
            with open(self.callbacks, encoding="utf-8") as stream:
                return [json.loads(line) for line in stream]
        except FileNotFoundError:
            return []


def shape(lines: list[str]) -> list[str]:
    return [
        "turn end"
        if TURN_END.fullmatch(line)
        else m[1]
        if (m := HARNESS_LINE.fullmatch(line))
        else line
        for line in lines
    ]


class Tui(Detach):
    def test_from_the_manager_shown_in_its_grid_turn_ends_then_one_outcome_done(self):
        res = self.detach(mode="tui", steps=[START], turns=[[block(DONE)]], inside=MANAGER)
        early = shape(live_tmux.events(self.events))
        record = self.said(res, tui=True)
        self.assertNotIn("outcome done", early)
        self.until(
            lambda: live_tmux.assert_grid(self, self.server, MANAGER, [[TUI]]),
            f"the grid [[{TUI!r}]]",
        )
        shaped = self.ended(turn_ends=2)
        self.assertEqual(shaped.count("turn end"), 2)
        self.assertEqual(self.harness_lines(shaped), ["outcome done", "exited 0"])
        self.assertEqual(self.read(self.out), DONE["deliverable"])
        self.assertEqual(self.last("result")["outcome"]["status"], "done")
        self.assertTrue(self.live(TUI))
        self.assertTrue(record.display.startswith(MANAGER), record.display)

    def test_outside_any_client_runs_shown_in_no_pane(self):
        res = self.detach(mode="tui", steps=[START], turns=[[block(DONE)]])
        self.said(res, tui=True)
        shaped = self.ended(turn_ends=2)
        self.assertEqual(self.harness_lines(shaped), ["outcome done", "exited 0"])
        self.assertEqual(self.read(self.out), DONE["deliverable"])
        self.assertTrue(self.live(TUI))
        clients = self.server.tmux("list-clients", "-t", f"={TUI}")
        self.assertEqual((clients.returncode, clients.stdout), (0, ""), clients.stderr)
        self.assertEqual(self.server.option(TUI, "@pane"), "")
        self.assertEqual(len(self.server.panes(f"={MANAGER}:")), 1)

    def test_a_policy_env_secret_reaches_the_client_but_no_argv_record_or_printed_command(self):
        with open(os.path.join(self.ensemble, "config.local.toml"), "w", encoding="utf-8") as f:
            f.write(f'[delegate.policy.claude]\nenv = {{ FAKE_TOKEN = "{SECRET}" }}\n')
        launched = os.path.join(self.dir, "launched.jsonl")
        dump = (
            "import json, os, sys; f = open(sys.argv[1], 'a'); "
            "f.write(json.dumps({'argv': sys.argv[2:], 'env': dict(os.environ)}) + chr(10))"
        )
        python, fake = shlex.quote(sys.executable), shlex.quote(fake_claude.__file__)
        with open(os.path.join(self.server.bin, "claude"), "w", encoding="utf-8") as f:
            f.write(
                f'#!/bin/sh\n{python} -I -c "{dump}" {shlex.quote(launched)} "$@"\n'
                f'exec {python} -I {fake} "$@"\n'
            )
        res = self.detach(mode="tui", steps=[START], turns=[[block(DONE)]])
        self.said(res, tui=True)
        self.until(lambda: self.kinds().count("session") > 0, "a session row")

        def tui_launches():
            with contextlib.suppress(FileNotFoundError), open(launched, encoding="utf-8") as f:
                whole = [json.loads(line) for line in f if line.endswith("\n")]
                return [e["env"] for e in whole if "--session-id" in e["argv"]]
            return []

        self.until(tui_launches, "the tui claude's launch record")
        self.assertEqual([env.get("FAKE_TOKEN") for env in tui_launches()], [SECRET])
        ps = subprocess.run(["ps", "-A", "-ww", "-o", "args="], capture_output=True, text=True)
        self.assertEqual(ps.returncode, 0, ps.stderr)
        self.ended(turn_ends=2)
        printed = [res.stdout, res.stderr, ps.stdout]
        for layer, name in (("delegate", self.work), ("session", TUI)):
            for query in ("status", "attach-command"):
                out = self.cli([*PY, "-m", f"ensemble.{layer}.cli", query, name])
                self.assertEqual(out.returncode, 0, out.stderr)
                printed += [out.stdout, out.stderr]
        options = self.server.tmux("show-options", "-t", f"={TUI}:")
        self.assertIn("@kind terminal", options.stdout)
        printed += [options.stdout, self.read(os.path.join(Layout(self.work).run, "run.jsonl"))]
        for text in printed:
            self.assertNotIn(SECRET, text)
        resume = self.last("session")["resume"]
        recorder = os.path.join(self.dir, "rec")
        os.mkdir(recorder)
        with open(os.path.join(recorder, "claude"), "w", encoding="utf-8") as f:
            f.write(
                f"#!{sys.executable}\nimport json, os, sys\n"
                "with open(os.environ['RESUME_RECORD'], 'w') as f:\n"
                "    json.dump({'argv': sys.argv[1:], 'env': dict(os.environ)}, f)\n"
            )
        os.chmod(os.path.join(recorder, "claude"), 0o700)
        record = os.path.join(self.dir, "resumed.json")
        env = self.server.env(RESUME_RECORD=record)
        env["PATH"] = recorder + os.pathsep + env["PATH"]
        run = subprocess.run(["/bin/sh", "-c", resume], env=env, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        with open(record, encoding="utf-8") as f:
            resumed = json.load(f)
        self.assertEqual(
            (resumed["argv"][:2], resumed["env"].get("FAKE_TOKEN")), (["--resume", SID], SECRET)
        )
        self.assertNotIn(SECRET, "\0".join(resumed["argv"]))

    def test_kill_session_of_the_harness_stops_the_tui_one_outcome_error(self):
        res = self.detach(mode="tui", steps=[START])
        self.said(res, tui=True)
        self.until(lambda: "nudge" in self.kinds(), "the nudge")
        shown = self.server.tmux("display-message", "-p", "-t", f"={HARNESS}:", "#{pane_pid}")
        pid = int(shown.stdout)
        self.assertEqual(self.server.tmux("kill-session", "-t", f"={HARNESS}").returncode, 0)
        self.until(lambda: not self.live(TUI), f"{TUI} stopped")
        self.until(lambda: not running(pid), f"the harness ({pid}) ended")
        outcomes = [s for s in shape(live_tmux.events(self.events)) if s.startswith("outcome")]
        self.assertEqual(outcomes, ["outcome error"])
        self.assertEqual(self.kinds()[-2:], ["result", "end"])
        rc = 128 + signal.SIGHUP
        self.assertEqual(
            (self.last("result")["error"], self.last("end")["rc"]),
            (f"stopped: Stopped: {rc}", rc),
        )

    def test_the_launchers_events_file_gets_the_turn_ends_and_one_outcome(self):
        res = self.detach(mode="tui", steps=[START], turns=[[block(DONE)]], launcher=True)
        self.said(res, tui=True)
        shaped = self.ended(turn_ends=2)
        self.assertEqual(shaped.count("turn end"), 2)
        self.assertEqual(self.harness_lines(shaped), ["outcome done", "exited 0"])

    def test_an_events_path_tmux_would_misread_exits_2_starting_nothing(self):
        events = os.path.join(self.dir, "ev#1")
        res = self.detach(mode="tui", steps=[START], events=events)
        self.assertEqual((res.returncode, res.stdout), (2, ""), res.stderr)
        self.assertIn(f"delegate: events file {events}: tmux would misread it\n", res.stderr)
        self.assertFalse(self.live(HARNESS) or self.live(TUI))
        self.assertFalse(os.path.exists(events))
        self.assertEqual(os.listdir(self.work), [])


class Headless(Detach):
    def test_sigterm_immediately_after_detach_exits_stopped_with_one_outcome(self):
        res = self.detach(steps=[START])
        self.said(res, tui=False)
        shown = self.server.tmux("display-message", "-p", "-t", f"={HARNESS}:", "#{pane_pid}")
        os.kill(int(shown.stdout), signal.SIGTERM)
        self.assertEqual(self.ended(), ["outcome error", "exited 143"])

    def test_done_one_outcome_line_the_harness_session_gone(self):
        res = self.detach(steps=[START, block(DONE)])
        self.said(res, tui=False)
        self.assertEqual(self.ended(), ["outcome done", "exited 0"])
        self.assertEqual(self.read(self.out), DONE["deliverable"])
        self.assertEqual(self.last("result")["outcome"]["status"], "done")

    def test_on_event_gets_each_event_from_the_run_dir_its_stdout_off_the_record_line(self):
        res = self.detach(*self.on_event(), steps=[START, block(DONE)])
        self.said(res, tui=False)
        self.assertNotIn(callback_recorder.NOISE, res.stdout + res.stderr)
        self.assertEqual(self.ended(), ["outcome done", "exited 0"])
        entries = self.delivered()
        events = [DelegateEvent.parse(entry["line"]) for entry in entries]
        self.assertEqual([event.kind for event in events], ["begun", "progress", "finished"])
        self.assertEqual({(event.sid, event.attempt) for event in events}, {(SID, 1)})
        self.assertEqual(events[-1].outcome.status, "done")
        run_dir = os.path.realpath(Layout(self.work).run)
        self.assertEqual({entry["cwd"] for entry in entries}, {run_dir})
        self.assertEqual(self.kinds().count("callback_error"), 0)

    def test_sigterm_while_the_callback_waits_kills_its_group_then_a_stopped_finished(self):
        res = self.detach(*self.on_event("--block", "progress"), steps=[START, block(DONE)])
        self.said(res, tui=False)
        (blocked,) = live_tmux.wait(
            lambda: [e for e in self.delivered() if json.loads(e["line"])["kind"] == "progress"],
            WAIT,
            what="the blocked progress callback",
        )
        self.assertTrue(all(running(blocked[pid]) for pid in ("pid", "grandchild")))
        shown = self.server.tmux("display-message", "-p", "-t", f"={HARNESS}:", "#{pane_pid}")
        os.kill(int(shown.stdout), signal.SIGTERM)
        self.assertEqual(self.ended(), ["outcome error", "exited 143"])
        for pid in ("pid", "grandchild"):
            live_tmux.wait(lambda pid=pid: not running(blocked[pid]), WAIT, what=f"{pid} gone")
        entries = self.delivered()
        events = [DelegateEvent.parse(entry["line"]) for entry in entries]
        self.assertEqual([event.kind for event in events], ["begun", "progress", "finished"])
        finished = events[-1]
        self.assertEqual(
            (finished.kind, finished.returncode, finished.outcome, finished.error),
            ("finished", 143, None, "stopped: Stopped: 143"),
        )
        self.assertFalse(set(entries[-1]["sigmask"]) & set(SIGNALS), entries[-1]["sigmask"])

    def test_a_sigkilled_harness_leaves_its_callback_group_until_resume_kills_it_first(self):
        res = self.detach(*self.on_event("--block", "progress:1"), steps=[START, block(DONE)])
        self.said(res, tui=False)
        (blocked,) = live_tmux.wait(
            lambda: [e for e in self.delivered() if json.loads(e["line"])["kind"] == "progress"],
            WAIT,
            what="the blocked progress callback",
        )
        os.kill(blocked["ppid"], signal.SIGKILL)
        self.until(lambda: not self.live(HARNESS), f"{HARNESS} gone")
        self.assertTrue(all(running(blocked[pid]) for pid in ("pid", "grandchild")))
        resumed = self.cli([*RESUME, self.work])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertFalse(any(running(blocked[pid]) for pid in ("pid", "grandchild")))
        self.until(lambda: "result" in self.kinds(), "the resumed run's result")
        self.assertEqual(self.last("result")["outcome"]["status"], "done")
        with open(os.path.join(Layout(self.work).run, "delegate.json")) as f:
            self.assertEqual(json.load(f)["attempt"], 2)
        self.until(lambda: not self.live(HARNESS), f"the resumed {HARNESS} gone")

    def test_the_launchers_events_file_gets_one_outcome_line(self):
        res = self.detach(steps=[START, block(DONE)], launcher=True)
        self.said(res, tui=False)
        self.assertEqual(self.ended(), ["outcome done", "exited 0"])


class Answer(Detach):
    def answer(self, *, steps, turns=()) -> subprocess.CompletedProcess:
        with open(self.scenario, "w", encoding="utf-8") as stream:
            json.dump({"steps": steps, "turns": list(turns)}, stream)
        self.answer_offset = len(live_tmux.events(self.events_used))
        return self.cli([*RESUME, self.work, "--input", ANSWER])

    def answered(self, record: Record, *, turn_ends: int) -> None:
        """record's run resumed SID with ANSWER to done; a resume then starts nothing."""
        self.ended()
        shaped = shape(live_tmux.events(self.events_used)[self.answer_offset :])
        self.assertEqual(shaped.count("turn end"), turn_ends)
        self.assertEqual(self.harness_lines(shaped), ["outcome done", "exited 0"])
        self.assertEqual(self.read(self.out), DONE["deliverable"])
        self.assertEqual(self.attempt(), 2)
        records = self.records()
        self.assertEqual([e["text"] for e in records if e["kind"] == "input"], ["Hello.", ANSWER])
        self.assertEqual(
            {(e["sid"], e["cwd"]) for e in records if e["kind"] == "session"},
            {(SID, Layout(self.work).agent)},
        )
        again = self.cli([*RESUME, self.work])
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(Record.parse(again.stdout), record)
        self.assertFalse(self.live(HARNESS))
        self.assertEqual(shape(live_tmux.events(self.events_used)[self.answer_offset :]), shaped)

    def attempt(self) -> int:
        with open(os.path.join(Layout(self.work).run, "delegate.json")) as f:
            return json.load(f)["attempt"]

    def test_headless_needs_input_answered_by_a_detached_resume(self):
        self.said(self.detach(steps=[block(ASKING)]), tui=False)
        self.assertEqual(self.ended(), ["outcome needs_input", "exited 0"])
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(self.last("result")["outcome"]["questions"], ASKING["questions"])
        res = self.answer(steps=[START, block(DONE)])
        self.answered(self.said(res, tui=False), turn_ends=0)

    def test_tui_needs_input_answered_by_a_detached_resume_closing_its_open_tui(self):
        res = self.detach(mode="tui", steps=[START], turns=[[block(ASKING)]])
        self.said(res, tui=True)
        shaped = self.ended(turn_ends=2)
        self.assertEqual(self.harness_lines(shaped), ["outcome needs_input", "exited 0"])
        self.assertTrue(self.live(TUI))
        res = self.answer(steps=[START], turns=[[block(DONE)]])
        self.answered(self.said(res, tui=True), turn_ends=2)

    def test_an_answer_while_the_run_lives_exits_2_starting_nothing(self):
        self.said(self.detach(steps=[START], hang=True), tui=False)
        self.until(lambda: "progress" in self.kinds(), "the client's start mark")
        res = self.answer(steps=[START, block(DONE)])
        self.assertEqual((res.returncode, res.stdout), (2, ""), res.stderr)
        self.assertIn("delegate is still alive", res.stderr)
        self.assertTrue(self.live(HARNESS))
        self.assertEqual(self.attempt(), 1)
        shown = self.server.tmux("display-message", "-p", "-t", f"={HARNESS}:", "#{pane_pid}")
        self.assertEqual(self.server.tmux("kill-session", "-t", f"={HARNESS}").returncode, 0)
        self.until(lambda: not running(int(shown.stdout)), "the harness ended")


if __name__ == "__main__":
    unittest.main()
