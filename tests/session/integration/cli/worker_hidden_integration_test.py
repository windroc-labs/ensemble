"""A worker's worker pane and hidden state through bin/ensemble-session on a private tmux server."""

import os
import re
import shlex
import shutil
import signal
import tempfile

from ensemble.session.hosts.base import worker_state
from ensemble.utils import Record
from tests.session import harnesses, live_tmux
from tests.session.case import REPO
from tests.session.cli_support import MANAGER, CliCase, guard_command
from tests.session.integration.clients.codex import fake_tui
from tests.utils.sandbox import pid_exists as pid_alive

WINDOW = f"={MANAGER}:"
OTHER = "elsewhere"


class HiddenCase(CliCase):
    def count(self) -> int:
        return len(self.server.panes(WINDOW))

    def hidden(self, name: str = "w1", **kw) -> bool:
        (found,) = self.status(name, **kw)
        return found.hidden

    def pane(self, name: str = "w1") -> str:
        """Its worker pane, "" if none."""
        return self.server.pane_of(name)

    def shows(self, name: str = "w1", window: str = WINDOW) -> str:
        """Its worker pane, once one in window runs its current guard's attach command."""

        def found():
            pane = self.pane(name)
            if pane not in self.server.panes(window):
                return None
            started = self.server.tmux("display-message", "-p", "-t", pane, "#{pane_start_command}")
            return pane if shlex.split(started.stdout) == shlex.split(guard_command(name)) else None

        return live_tmux.wait(found, what=f"{name}'s worker pane in {window}")

    def again(self, cmd: str, name: str = "w1", **kw):
        return self.session(cmd, name, **self.scene(name), **kw)

    def crash(self, name: str = "w1") -> None:
        """kill -9 of its session harness (its guard ends the client)."""
        channel = self.owner(name)
        os.kill(channel.status()["pid"], signal.SIGKILL)
        live_tmux.wait(lambda: not channel.alive(), what="the killed harness to let go")


class HiddenStartTest(HiddenCase):
    def test_display_none_starts_without_a_pane_hidden_and_opened_by_the_caller(self):
        before = self.count()
        record = self.record(self.start("w1", "--display", "none"))
        self.assertEqual(record.display, f"{MANAGER}///hidden")
        self.assertEqual((self.count(), self.pane()), (before, ""))
        self.assertEqual(self.server.opener_of("w1"), "")
        self.assertEqual(self.server.sessions(), [MANAGER])
        self.assertTrue(self.hidden())
        listed = self.ok(self.session("ls", "--here", "--records")).stdout.splitlines()
        self.assertIn(record, [Record.parse(line) for line in listed])

    def test_a_shown_display_none_worker_stays_shown_after_restart(self):
        before = self.count()
        self.record(self.start("w1", "--display", "none"))
        self.wait_event("w1", "done")
        self.assertEqual((self.count(), self.hidden()), (before, True))
        self.ok(self.session("show", "w1"))
        self.shows()
        self.assertFalse(self.hidden())
        self.record(self.again("restart"))
        self.shows()
        self.assertFalse(self.hidden())

    def test_a_hidden_worker_stays_hidden_after_restart_and_resume(self):
        self.record(self.start("w1"))
        self.wait_event("w1", "done")
        self.shows()
        self.ok(self.session("hide", "w1"))
        before = self.count()
        self.record(self.again("restart"))
        self.assertEqual((self.count(), self.pane()), (before, ""))
        self.assertTrue(self.hidden())
        self.crash()
        self.record(self.again("resume"))
        self.assertEqual((self.count(), self.pane()), (before, ""))
        self.assertTrue(self.hidden())

    def test_a_new_start_of_the_same_sid_takes_its_own_view(self):
        record = self.record(self.start("w1"))
        self.wait_event("w1", "done")
        self.ok(self.session("hide", "w1"))
        self.ok(self.session("close", "w1"))
        self.record(self.start("w1", "--resume", record.sid))
        self.shows()
        self.assertFalse(self.hidden())
        self.ok(self.session("close", "w1"))
        self.record(self.start("w1", "--resume", record.sid, "--display", "none"))
        self.assertEqual(self.pane(), "")
        self.assertTrue(self.hidden())

    def test_start_refuses_a_given_hidden_part(self):
        res = self.start("w1", "--display", f"{MANAGER}///hidden")
        self.assertEqual((res.returncode, res.stdout), (2, ""), res.stderr)
        self.assertIn("its hidden part comes only from none", res.stderr)
        self.assertFalse(live_tmux.running("w1"))
        self.assertEqual(self.server.sessions(), [MANAGER])


class HookTest(HiddenCase):
    def kill_pane(self, name: str, window: str = WINDOW) -> str:
        pane = self.shows(name, window)
        self.ok(self.server.tmux("kill-pane", "-t", pane))
        return pane

    def control(self, name: str = "c1", window: str = WINDOW, **kw) -> None:
        """Kills name's worker pane: once it shows hidden, earlier hooks have run."""
        self.kill_pane(name, window)
        live_tmux.wait(lambda: self.hidden(name, **kw), what=f"{name} hidden")
        self.assertTrue(live_tmux.running(name))

    def test_hide_if_closed_changes_a_shown_worker_nothing(self):
        self.record(self.start("w1"))
        pane = self.shows()
        self.assertEqual(self.ok(self.session("hide", "--if-closed", "w1")).stdout, "")
        self.assertEqual((self.shows(), self.hidden()), (pane, False))
        self.assertEqual(self.session("hide", "--if-closed", "w9").returncode, 1)

    def test_kill_pane_marks_hidden_and_resume_keeps_it_hidden(self):
        """kill-pane of w1's worker pane marks it hidden; a resume of it keeps it hidden."""
        self.record(self.start("w1"))
        self.record(self.start("w2"))
        self.wait_event("w1", "done")
        pid = self.calls("w1")[0]["pid"]
        beside = self.shows("w2")
        manager = self.server.inside(MANAGER)["TMUX_PANE"]
        self.kill_pane("w1")
        live_tmux.wait(self.hidden, what="w1 hidden")
        self.assertEqual(self.pane(), "")
        self.assertTrue(live_tmux.running("w1"))
        self.assertTrue(pid_alive(pid))
        live_tmux.wait(
            lambda: live_tmux.assert_grid(self, self.server, MANAGER, [["w2"]]),
            what="the grid retiled without w1",
        )
        self.record(self.again("resume"))
        self.assertEqual(self.pane(), "")
        self.assertTrue(self.hidden())
        self.assertEqual(set(self.server.panes(WINDOW)), {manager, beside})
        self.assertTrue(pid_alive(pid))

    def entries(self) -> set[str]:
        out = self.ok(self.server.tmux("show-hooks", "-w", "-t", WINDOW, "after-kill-pane")).stdout
        return {f"%{m[1]}" for m in re.finditer(r"^after-kill-pane\[([0-9]+)\] ", out, re.M)}

    def exited(self) -> tuple[str, str]:
        """w1's pane and generation once its run ends and pane exits, its hook left."""
        pane, generation = self.shows(), worker_state("w1").generation
        self.crash()
        live_tmux.wait(lambda: pane not in self.server.panes(WINDOW), what="w1's pane to exit")
        live_tmux.wait(lambda: not self.status("w1")[0].alive, what="w1's run to end")
        self.assertIn(pane, self.entries())
        return pane, generation

    def hide(self, sid: str, generation: str):
        return self.session("hide", "--if-closed", "--sid", sid, "--generation", generation, "w1")

    def test_an_ended_runs_close_hook_leaves_its_resume_shown(self):
        """Exit, kill-pane of another pane, resume: w1 shown, its stale entry pruned."""
        record = self.record(self.start("w1"))
        self.record(self.start("w2"))
        self.wait_event("w1", "done")
        gone, generation = self.exited()
        self.assertEqual(self.hide(record.sid, generation).returncode, 1)
        self.control("w2")
        self.assertFalse(self.hidden())
        self.record(self.again("resume"))
        self.shows()
        self.assertFalse(self.hidden())
        self.assertNotIn(gone, self.entries())
        self.assertLessEqual(self.entries(), set(self.server.panes(WINDOW)))

    def test_an_ended_run_reads_gone_once_its_pane_exits(self):
        """Not waiting for its run to end."""
        record = self.record(self.start("w1"))
        self.record(self.start("w2"))
        self.wait_event("w1", "done")
        pane, generation = self.shows(), worker_state("w1").generation
        self.crash()
        live_tmux.wait(lambda: pane not in self.server.panes(WINDOW), what="w1's pane to exit")
        self.assertFalse(self.status("w1")[0].alive)
        self.assertEqual(self.hide(record.sid, generation).returncode, 1)
        self.control("w2")
        self.assertFalse(self.hidden())

    def test_an_ended_runs_close_hook_leaves_a_resumed_run_unhidden(self):
        """Exit, resume shown nowhere, kill-pane of another pane: w1 not hidden; another
        run's hide exits 1.
        """
        record = self.record(self.start("w1"))
        self.record(self.start("w2"))
        self.wait_event("w1", "done")
        _, ended = self.exited()
        self.record(self.again("resume", inside=None))
        self.assertEqual(self.pane(), "")
        self.control("w2")
        self.assertFalse(self.hidden())
        self.assertTrue(live_tmux.running("w1"))
        generation = worker_state("w1").generation
        self.assertNotEqual(generation, ended)
        for sid, run in ((record.sid, ended), ("other", generation)):
            with self.subTest(sid=sid, generation=run):
                self.assertEqual(self.hide(sid, run).returncode, 1)
                self.assertFalse(self.hidden())
        self.ok(self.hide(record.sid, generation))
        self.assertTrue(self.hidden())

    def test_losing_the_managers_window_marks_nothing(self):
        self.tmux("new-session", "-d", "-s", OTHER, "-x", "200", "-y", "50", *live_tmux.IDLE)
        self.server.attach(OTHER)
        self.record(self.start("w1"))
        self.shows()
        self.record(self.start("w2"))
        self.shows("w2")
        self.ok(self.session("hide", "w2"))
        self.record(self.start("c1", inside=OTHER))
        self.ok(self.server.tmux("kill-window", "-t", WINDOW))
        self.control(window=f"={OTHER}:", inside=OTHER)
        self.assertFalse(self.hidden(inside=OTHER))
        self.assertTrue(self.hidden("w2", inside=OTHER))

    def tmux(self, *args: str) -> None:
        self.ok(self.server.tmux(*args))


class CodexHiddenTest(HiddenCase):
    def setUp(self):
        super().setUp()
        # Codex may write /tmp and $TMPDIR, where the fixture's HOME lies; its state lies elsewhere
        root = tempfile.mkdtemp(dir=os.environ.get("ENSEMBLE_TEST_TMP", REPO))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.addCleanup(self.server.kill)
        home, bin_dir = os.path.join(root, "home"), os.path.join(root, "bin")
        self.addCleanup(harnesses.end, home)
        for path in (home, bin_dir):
            os.mkdir(path)
        shutil.copy2(self.server.program, bin_dir)
        fake_tui.install(bin_dir)
        # hooks run with the server's environment: one HOME
        self.ok(self.server.tmux("set-environment", "-g", "HOME", home))
        scenario = os.path.join(root, "w1.codex.json")
        with open(scenario, "w", encoding="utf-8") as f:
            f.write('{"tui_turns": [["Hi from w1."]]}')
        self.extra = {
            "HOME": home,
            "PATH": bin_dir + os.pathsep + self.env()["PATH"],
            "FAKE_SCENARIO": scenario,
            "FAKE_LOG": os.path.join(root, "w1.codex.jsonl"),
        }

    def codex(self, cmd: str, *args: str):
        if cmd != "start":
            return self.session(cmd, "w1", *args, **self.extra)
        argv = ("start", "w1", "--client", "codex", "--cwd", self.cwd, "--prompt", "hi", *args)
        return self.session(*argv, ENSEMBLE_EVENTS=self.events, **self.extra)

    def test_a_codex_worker_hides_shows_and_restarts_like_claude(self):
        before = self.count()
        record = self.record(self.codex("start", "--display", "none"))
        self.assertEqual((record.detail, record.display), ("codex", f"{MANAGER}///hidden"))
        self.assertEqual((self.count(), self.pane()), (before, ""))
        self.assertTrue(self.hidden(**self.extra))
        self.ok(self.codex("show"))
        self.ok(self.server.tmux("kill-pane", "-t", self.shows()))
        live_tmux.wait(lambda: self.hidden(**self.extra), what="w1 hidden")
        self.ok(self.codex("show"))
        self.shows()
        self.assertFalse(self.hidden(**self.extra))
        self.record(self.codex("restart"))
        self.shows()
        self.assertFalse(self.hidden(**self.extra))
        self.ok(self.codex("hide"))
        self.assertEqual(self.count(), before)
        self.assertTrue(self.hidden(**self.extra))
