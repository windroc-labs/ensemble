"""`delegate run --detach` over fake_claude: a tui run on a private tmux server, a headless one
with none; the drive is a local ProcessControl process, never a tmux session.
"""

import contextlib
import glob
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import unittest

from ensemble.delegate import DelegateEvent
from ensemble.delegate.engine.types import Layout
from ensemble.session import SIGNALS
from ensemble.utils import ProcessControl, Record, Status
from tests.delegate import drives
from tests.delegate.case import PY, PYTHONPATH, Case
from tests.delegate.fakes import callback_recorder
from tests.delegate.integration.cli import ASKING, DONE, RUN, SID
from tests.delegate.replies import block, mark
from tests.session import live_tmux
from tests.session.case import PLUGIN_BIN
from tests.session.integration.clients.claude import fake_claude
from tests.utils.sandbox import pid_exists as running

MANAGER = "mgr"
ROLE = "dummy-tester"
TUI = f"{ROLE}-{SID[:8]}"
DRIVE = f"{TUI}-drive"
HARNESS_ARGV = ("-m", "ensemble.delegate.engine.harness")
STAMP = "[0-9]{2}:[0-9]{2}:[0-9]{2}"
TURN_END = re.compile(rf"{STAMP} {TUI} done")
DRIVE_LINE = re.compile(rf"{STAMP} {DRIVE} ((?:outcome [a-z_]+)|(?:exited -?[0-9]+))")
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
        self.server = None
        self.proj, self.work, self.bin, self.no_tmux = (
            os.path.join(self.dir, d) for d in ("proj", "work", "bin", "tmux")
        )
        for d in (self.proj, self.work, self.bin, self.no_tmux):
            os.mkdir(d)
        self.path = fake_claude.install(
            self.bin,
            tail=os.pathsep.join((PLUGIN_BIN, os.path.dirname(sys.executable), "/usr/bin", "/bin")),
        )
        self.out = os.path.join(self.work, "out.md")
        self.events, self.scenario = (
            os.path.join(self.dir, f) for f in ("events", "scenario.json")
        )
        self.addCleanup(drives.end, self.dir)

    def tmux(self) -> live_tmux.Server:
        """The private tmux server a tui run needs, started on first use."""
        if self.server is None:
            self.server = live_tmux.Server(self, self.home)
            self.server.start(MANAGER)
            self.server.attach(MANAGER)
        return self.server

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
        ensemble-crew run gives them. A tui run starts the tmux server; a headless one has none.
        """
        if mode == "tui" or inside:
            self.tmux()
        with open(self.scenario, "w") as f:
            json.dump({"steps": steps, "turns": list(turns), **scenario}, f)
        self.events_used = events or self.events
        given = {"ENSEMBLE_EVENTS": self.events_used, "ENSEMBLE_SID": SID}
        argv = [*RUN, "--role", ROLE, "--input", text, "--out", self.out, "--workdir"]
        argv += [self.work, "--mode", mode, "--detach", *extra]
        argv += [] if launcher else ["--sid", SID, "--events", self.events_used]
        return self.cli(argv, inside=inside, given=given if launcher else {})

    def cli(self, argv, *, inside=None, given=None) -> subprocess.CompletedProcess:
        """On the tmux server once one started, else with a private TMUX_TMPDIR no server uses."""
        extra = {
            "PYTHONPATH": PYTHONPATH,
            "CLAUDE_CONFIG_DIR": os.path.join(self.dir, "config"),
            fake_claude.ENV: self.scenario,
            **(self.server.inside(inside) if inside else {}),
            **(given or {}),
        }
        if self.server is not None:
            env = self.server.env(**extra)
        else:
            root = os.environ.get("ENSEMBLE_CONTROL_ROOT")
            env = {
                "PATH": self.path,
                "HOME": self.home,
                "PYTHONUTF8": "1",
                "TMUX_TMPDIR": self.no_tmux,
                **({"ENSEMBLE_CONTROL_ROOT": root} if root else {}),
                **extra,
            }
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
        """The Record line; its sessions and drive (tui: unless its attempt ended) live on return;
        the drive's log 0600.
        """
        drive = self.drive_running()
        live = {TUI: self.live(TUI)} if tui else {}
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(res.stdout.splitlines()), 1, res.stdout)
        record = Record.parse(res.stdout)
        sessions = (TUI,) if tui else ()
        self.assertEqual(
            (record.kind, record.name, record.sessions, record.sid, record.workdir),
            ("role", TUI, sessions, SID, self.work),
        )
        self.assertEqual(
            (record.ctl, record.resume),
            (("ensemble-delegate",), ("ensemble-delegate", "resume", self.work)),
        )
        self.assertTrue(all(live.values()), live)
        ended = tui and self.metadata().get("result") is not None
        self.assertTrue(drive or ended, self.metadata())
        self.assertEqual(self.metadata()["detached"], True)
        if tui:
            self.assertFalse(self.live(DRIVE))
        log = os.lstat(Layout(self.work).drive_log)
        self.assertEqual((stat.S_ISREG(log.st_mode), stat.S_IMODE(log.st_mode)), (True, 0o600))
        self.assertIn(f"delegate: session {SID}\n", res.stderr)
        return record

    def metadata(self) -> dict:
        with open(os.path.join(Layout(self.work).run, "delegate.json")) as f:
            return json.load(f)

    def control(self) -> ProcessControl | None:
        """delegate.json's drive; None before a run saved one."""
        try:
            drive = self.metadata().get("drive")
        except FileNotFoundError:
            return None
        return None if drive is None else ProcessControl.attach(drive)

    def drive_running(self) -> bool:
        control = self.control()
        return control is not None and control.running()

    def drive_log(self) -> str:
        try:
            return self.read(Layout(self.work).drive_log)
        except FileNotFoundError:
            return "<no drive.log>"

    def drive_group(self) -> tuple[int, int]:
        """The drive's harness process: its pid and process group, never this test's."""
        ps = subprocess.run(
            ["ps", "-A", "-ww", "-o", "pid=,pgid=,args="], capture_output=True, text=True
        )
        self.assertEqual(ps.returncode, 0, ps.stderr)
        tail = " ".join((*HARNESS_ARGV, self.work))
        rows = [line.split(None, 2) for line in ps.stdout.splitlines()]
        found = [(int(row[0]), int(row[1])) for row in rows if row[2:] and row[2].endswith(tail)]
        self.assertEqual(len(found), 1, ps.stdout)
        ((pid, group),) = found
        self.assertNotEqual(group, os.getpgrp())
        return pid, group

    def kill_drive(self) -> None:
        """SIGKILL to the drive's process group; returns once its control saw the group end."""
        _, group = self.drive_group()
        os.killpg(group, signal.SIGKILL)
        drives.stop(self.control())

    def poke(self) -> None:
        # tmux on Linux (CI: 3.4) can leave an exited pane's process unreaped until another child of
        # the server exits
        if self.server is not None:
            self.server.tmux("run-shell", "-b", "true")

    def live(self, name: str) -> bool:
        return self.server.tmux("has-session", "-t", f"={name}").returncode == 0

    def until(self, check, what: str, *, diagnostics=None):
        """diagnostics: a timeout's AssertionError ends with its text (default: drive.log)."""

        def poked():
            self.poke()
            return check()

        try:
            return live_tmux.wait(poked, WAIT, what=what)
        except AssertionError as timeout:
            told = (diagnostics or self.drive_log)()
            raise AssertionError(f"{timeout}; {told}") from None

    def ended(self, turn_ends: int = 0) -> list[str]:
        """The shaped events once they hold the drive's exited line and turn_ends turn ends."""

        def check():
            shaped = shape(live_tmux.events(self.events_used))
            return (
                any(s.startswith("exited ") for s in shaped)
                and shaped.count("turn end") >= turn_ends
            )

        self.until(check, f"`{DRIVE} exited` and {turn_ends} turn ends")
        self.until(lambda: not self.drive_running(), f"{DRIVE} ended")
        shaped = shape(live_tmux.events(self.events_used))
        statuses = [s.removeprefix("outcome ") for s in shaped if s.startswith("outcome ")]
        if statuses and statuses[-1] != "error":
            self.assertIn(f"delegate: status {statuses[-1]}\n", self.drive_log())
        if self.server is None:
            sockets = glob.glob(os.path.join(self.no_tmux, "*", "*"))
            self.assertEqual([p for p in sockets if stat.S_ISSOCK(os.lstat(p).st_mode)], [])
        return shaped

    def drive_lines(self, shaped: list[str]) -> list[str]:
        return [line for line in shaped if line != "turn end"]

    def records(self):
        """run.jsonl's complete lines; a line still being written waits for a later read."""
        try:
            with open(os.path.join(Layout(self.work).run, "run.jsonl")) as stream:
                return [json.loads(line) for line in stream if line.endswith("\n")]
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
        if (m := DRIVE_LINE.fullmatch(line))
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
        self.assertEqual(self.drive_lines(shaped), ["outcome done", "exited 0"])
        self.assertEqual(self.read(self.out), DONE["deliverable"])
        self.assertEqual(self.last("result")["outcome"]["status"], "done")
        self.assertTrue(self.live(TUI))
        self.assertTrue(record.display.startswith(MANAGER), record.display)

    def test_outside_any_client_runs_shown_in_no_pane(self):
        res = self.detach(mode="tui", steps=[START], turns=[[block(DONE)]])
        self.said(res, tui=True)
        shaped = self.ended(turn_ends=2)
        self.assertEqual(self.drive_lines(shaped), ["outcome done", "exited 0"])
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
        with open(os.path.join(self.tmux().bin, "claude"), "w", encoding="utf-8") as f:
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
        printed = [res.stdout, res.stderr, ps.stdout, self.drive_log()]
        for layer, name in (("delegate", self.work), ("session", TUI)):
            for query in ("status", "attach-command"):
                out = self.cli([*PY, "-m", f"ensemble.{layer}.cli", query, name])
                self.assertEqual(out.returncode, 0, out.stderr)
                printed += [out.stdout, out.stderr]
        options = self.server.tmux("show-options", "-t", f"={TUI}:")
        self.assertIn("@kind guard", options.stdout)
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

    def test_a_sigkilled_drive_group_leaves_the_owned_tui_and_its_attempt_and_no_drive_line(self):
        res = self.detach(mode="tui", steps=[START])
        self.said(res, tui=True)
        self.until(lambda: "nudge" in self.kinds(), "the nudge")
        self.kill_drive()
        self.assertTrue(self.live(TUI))
        lines = shape(live_tmux.events(self.events))
        self.assertEqual([s for s in lines if s.startswith(("outcome ", "exited "))], [])
        self.assertNotIn("result", self.kinds())
        self.assertEqual(self.metadata()["completed"], False)

    def test_the_launchers_events_file_gets_the_turn_ends_and_one_outcome(self):
        res = self.detach(mode="tui", steps=[START], turns=[[block(DONE)]], launcher=True)
        self.said(res, tui=True)
        shaped = self.ended(turn_ends=2)
        self.assertEqual(shaped.count("turn end"), 2)
        self.assertEqual(self.drive_lines(shaped), ["outcome done", "exited 0"])

    def test_an_events_path_tmux_would_misread_exits_2_starting_nothing(self):
        events = os.path.join(self.dir, "ev#1")
        res = self.detach(mode="tui", steps=[START], events=events)
        self.assertEqual((res.returncode, res.stdout), (2, ""), res.stderr)
        self.assertIn(f"delegate: events file {events}: tmux would misread it\n", res.stderr)
        self.assertFalse(self.live(TUI))
        self.assertEqual(glob.glob(os.path.join(self.home, "apm-group-*")), [])
        self.assertFalse(os.path.exists(events))
        self.assertEqual(os.listdir(self.work), [])


class Headless(Detach):
    def test_sigterm_immediately_after_detach_exits_stopped_with_one_outcome(self):
        res = self.detach(steps=[START])
        self.said(res, tui=False)
        self.control().terminate()
        self.assertEqual(self.ended(), ["outcome error", "exited 143"])

    def test_done_one_outcome_line_then_the_drive_ends(self):
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
        self.control().terminate()
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

    def test_a_sigkilled_drive_leaves_its_callback_group_until_resume_kills_it_first(self):
        res = self.detach(*self.on_event("--block", "progress:1"), steps=[START, block(DONE)])
        self.said(res, tui=False)
        (blocked,) = live_tmux.wait(
            lambda: [e for e in self.delivered() if json.loads(e["line"])["kind"] == "progress"],
            WAIT,
            what="the blocked progress callback",
        )
        self.assertEqual(blocked["ppid"], self.drive_group()[0])
        first = self.metadata()["drive"]
        os.kill(blocked["ppid"], signal.SIGKILL)
        self.until(lambda: not self.drive_running(), f"{DRIVE} ended")
        self.assertTrue(all(running(blocked[pid]) for pid in ("pid", "grandchild")))
        resumed = self.cli([*RESUME, self.work])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertFalse(any(running(blocked[pid]) for pid in ("pid", "grandchild")))
        self.until(lambda: "result" in self.kinds(), "the resumed run's result")
        self.assertEqual(self.last("result")["outcome"]["status"], "done")
        state = self.metadata()
        self.assertEqual(state["attempt"], 2)
        self.assertNotEqual(state["drive"], first)
        self.until(lambda: not self.drive_running(), f"the resumed {DRIVE} ended")

    def test_the_launchers_events_file_gets_one_outcome_line(self):
        res = self.detach(steps=[START, block(DONE)], launcher=True)
        self.said(res, tui=False)
        self.assertEqual(self.ended(), ["outcome done", "exited 0"])


class Answer(Detach):
    def answer(self, *, steps, turns=()) -> subprocess.CompletedProcess:
        with open(self.scenario, "w", encoding="utf-8") as stream:
            json.dump({"steps": steps, "turns": list(turns)}, stream)
        self.answer_offset = len(live_tmux.events(self.events_used))
        self.asked_drive = self.metadata()["drive"]
        return self.cli([*RESUME, self.work, "--input", ANSWER])

    def answered(self, record: Record, *, turn_ends: int) -> None:
        """record's run resumed SID with ANSWER to done on a drive of its own; a resume then
        starts nothing.
        """
        self.ended()
        shaped = shape(live_tmux.events(self.events_used)[self.answer_offset :])
        self.assertEqual(shaped.count("turn end"), turn_ends)
        self.assertEqual(self.drive_lines(shaped), ["outcome done", "exited 0"])
        self.assertEqual(self.read(self.out), DONE["deliverable"])
        self.assertEqual(self.attempt(), 2)
        drive = self.metadata()["drive"]
        self.assertNotEqual(drive, self.asked_drive)
        records = self.records()
        self.assertEqual([e["text"] for e in records if e["kind"] == "input"], ["Hello.", ANSWER])
        self.assertEqual(
            {(e["sid"], e["cwd"]) for e in records if e["kind"] == "session"},
            {(SID, Layout(self.work).agent)},
        )
        again = self.cli([*RESUME, self.work])
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(Record.parse(again.stdout), record)
        self.assertEqual(self.metadata()["drive"], drive)
        self.assertFalse(self.drive_running())
        self.assertEqual(shape(live_tmux.events(self.events_used)[self.answer_offset :]), shaped)

    def attempt(self) -> int:
        return self.metadata()["attempt"]

    def test_headless_needs_input_answered_by_a_detached_resume(self):
        self.said(self.detach(steps=[block(ASKING)]), tui=False)
        self.assertEqual(self.ended(), ["outcome needs_input", "exited 0"])
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(self.last("result")["outcome"]["questions"], ASKING["questions"])
        res = self.answer(steps=[START, block(DONE)])
        self.answered(self.said(res, tui=False), turn_ends=0)

    def test_tui_needs_input_answered_by_resume_input_in_its_live_pane(self):
        res = self.detach(mode="tui", steps=[START], turns=[[block(ASKING)], [block(DONE)]])
        record = self.said(res, tui=True)
        shaped = self.ended(turn_ends=2)
        self.assertEqual(self.drive_lines(shaped), ["outcome needs_input", "exited 0"])
        self.assertTrue(self.live(TUI))
        offset = len(live_tmux.events(self.events_used))
        drive = self.metadata()["drive"]
        res = self.cli([*RESUME, self.work, "--input", ANSWER])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(Record.parse(res.stdout), record)
        self.assertEqual(self.metadata()["drive"], drive)
        self.assertFalse(self.drive_running())
        self.until(
            lambda: "outcome done" in shape(live_tmux.events(self.events_used)[offset:]),
            "the answered attempt's outcome",
        )
        self.assertEqual(
            self.drive_lines(shape(live_tmux.events(self.events_used)[offset:])), ["outcome done"]
        )
        self.assertEqual(self.read(self.out), DONE["deliverable"])
        self.assertEqual(self.attempt(), 2)
        inputs = [e["text"] for e in self.records() if e["kind"] == "input"]
        self.assertEqual(inputs, ["Hello.", ANSWER])

    def test_an_answer_while_the_run_lives_exits_2_starting_nothing(self):
        self.said(self.detach(steps=[START], hang=True), tui=False)
        self.until(lambda: "progress" in self.kinds(), "the client's start mark")
        drive = self.metadata()["drive"]
        res = self.answer(steps=[START, block(DONE)])
        self.assertEqual((res.returncode, res.stdout), (2, ""), res.stderr)
        self.assertIn("delegate is still alive", res.stderr)
        self.assertEqual(self.metadata()["drive"], drive)
        self.assertTrue(self.drive_running())
        self.assertEqual(self.attempt(), 1)
        self.kill_drive()


class Owned(Detach):
    """A tui run its session harness owns: after its attempt ends, input typed into the live pane
    opens the next attempt, which the delegation handler drives to its own outcome.
    """

    CONTINUE = "continue"

    def send(self, text: str) -> None:
        res = self.cli([*PY, "-m", "ensemble.session.cli", "send", TUI, text])
        self.assertEqual(res.returncode, 0, res.stderr)

    def status(self) -> Status:
        res = self.cli([*PY, "-m", "ensemble.delegate.cli", "status", self.work])
        self.assertEqual(res.returncode, 0, res.stderr)
        return Status.parse(res.stdout)

    def test_needs_input_then_an_answer_in_the_pane_is_attempt_two(self):
        res = self.detach(mode="tui", steps=[START], turns=[[block(ASKING)], [block(DONE)]])
        self.said(res, tui=True)
        shaped = self.ended(turn_ends=2)
        self.assertEqual(self.drive_lines(shaped), ["outcome needs_input", "exited 0"])
        status = self.status()
        self.assertEqual((status.alive, status.outcome), (False, "needs_input"))
        self.assertTrue(self.live(TUI))
        offset = len(live_tmux.events(self.events_used))
        self.send(ANSWER)
        self.until(
            lambda: "outcome done" in shape(live_tmux.events(self.events_used)[offset:]),
            "attempt 2's outcome",
        )
        state = self.metadata()
        self.assertEqual(
            (state["attempt"], state["completed"], state["result"]["outcome"]["status"]),
            (2, True, "done"),
        )
        self.assertEqual(state["previous"]["outcome"]["status"], "needs_input")
        self.assertEqual(self.read(self.out), DONE["deliverable"])
        self.assertEqual(self.kinds().count("result"), 2)
        self.assertTrue(self.live(TUI))

    def test_continue_after_a_failed_turn_in_the_pane_is_the_next_attempt(self):
        failed = {"kind": "hook", "event": "StopFailure", "error": "server_overloaded"}
        res = self.detach(mode="tui", steps=[START, failed], turns=[[block(DONE)]])
        self.said(res, tui=True)
        self.ended()
        state = self.metadata()
        self.assertEqual(state["result"]["error"], "the client's turn failed: server_overloaded")
        self.assertFalse(self.status().alive)
        offset = len(live_tmux.events(self.events_used))
        self.send(self.CONTINUE)
        self.until(
            lambda: "outcome done" in shape(live_tmux.events(self.events_used)[offset:]),
            "the continued attempt's outcome",
        )
        state = self.metadata()
        self.assertEqual((state["attempt"], state["result"]["outcome"]["status"]), (2, "done"))


if __name__ == "__main__":
    unittest.main()
