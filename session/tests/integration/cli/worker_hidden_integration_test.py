"""A worker's view through bin/ensemble-session on a private tmux server: `start
--display none` opens no pane, the caller still its opener, and is hidden; show opens it and clears
hidden; restart and resume keep hidden or shown; a new start of the same sid takes its own view;
its events still reach the events file; a given hidden part is refused; a Codex worker the same.
Its viewer killed by hand marks it hidden (its client-detached hook); losing the manager's window,
or detaching the viewer and then another client, does not, each checked after a control worker's
killed viewer marks it hidden.
"""

import os
import shutil
import tempfile

from ensemble.utils import Record

from session.tests import live_tmux
from session.tests.case import REPO
from session.tests.cli_support import MANAGER, CliCase
from session.tests.integration.clients.codex import fake_tui

WINDOW = f"={MANAGER}:"
OTHER = "elsewhere"


class HiddenCase(CliCase):
    def count(self) -> int:
        return len(self.server.panes(WINDOW))

    def hidden(self, name: str = "w1", **kw) -> bool:
        (found,) = self.status(name, **kw)
        return found.hidden

    def viewer(self, name: str = "w1") -> str:
        """Its @pane, "" when show opened none for this tmux session."""
        return self.server.option(name, "@pane")

    def shows(self, name: str = "w1", window: str = WINDOW) -> str:
        """Its @pane, once that pane in window runs a client of it."""
        pane = self.viewer(name)
        self.assertIn(pane, self.server.panes(window))
        tty = self.server.tmux("display-message", "-p", "-t", pane, "#{pane_tty}").stdout.strip()

        def attached():
            found = self.server.tmux("list-clients", "-t", f"={name}", "-F", "#{client_tty}")
            return tty in found.stdout.split()

        live_tmux.wait(attached, what=f"{pane} to attach {name}")
        return pane

    def again(self, cmd: str, name: str = "w1", **kw):
        return self.session(cmd, name, **self.scene(name), **kw)

    def kill_session(self, name: str = "w1") -> None:
        self.ok(self.server.tmux("kill-session", "-t", f"={name}"))


class HiddenStartTest(HiddenCase):
    def test_display_none_starts_without_a_pane_hidden_and_opened_by_the_caller(self):
        before = self.count()
        record = self.record(self.start("w1", "--display", "none"))
        self.assertEqual(record.display, f"{MANAGER}///hidden")
        self.assertEqual((self.count(), self.viewer()), (before, ""))
        self.assertEqual(self.server.option("w1", "@opener"), "")
        self.assertTrue(self.hidden())
        listed = self.ok(self.session("ls", "--here", "--records")).stdout.splitlines()
        self.assertIn(record, [Record.parse(line) for line in listed])

    def test_show_opens_the_hidden_worker_and_clears_hidden(self):
        self.record(self.start("w1", "--display", "none"))
        self.ok(self.session("show", "w1"))
        self.shows()
        self.assertFalse(self.hidden())

    def test_a_shown_display_none_worker_stays_shown_after_restart(self):
        self.record(self.start("w1", "--display", "none"))
        self.wait_event("w1", "done")
        self.ok(self.session("show", "w1"))
        self.shows()
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
        self.assertEqual((self.count(), self.viewer()), (before, ""))
        self.assertTrue(self.hidden())
        self.kill_session()
        self.record(self.again("resume"))
        self.assertEqual((self.count(), self.viewer()), (before, ""))
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
        self.assertEqual(self.viewer(), "")
        self.assertTrue(self.hidden())

    def test_a_hidden_workers_events_reach_the_events_file_without_a_pane(self):
        before = self.count()
        self.record(self.start("w1", "--display", "none"))
        self.wait_event("w1", "done")
        self.assertEqual(self.count(), before)
        self.assertTrue(self.hidden())

    def test_a_default_start_opens_a_pane_not_hidden(self):
        self.record(self.start("w1"))
        self.shows()
        self.assertFalse(self.hidden())

    def test_start_refuses_a_given_hidden_part(self):
        res = self.start("w1", "--display", f"{MANAGER}///hidden")
        self.assertEqual((res.returncode, res.stdout), (2, ""), res.stderr)
        self.assertIn("its hidden part comes only from none", res.stderr)
        self.assertFalse(self.server.tmux("has-session", "-t", "=w1").returncode == 0)


class HookTest(HiddenCase):
    def kill_viewer(self, name: str, window: str = WINDOW) -> None:
        self.ok(self.server.tmux("kill-pane", "-t", self.shows(name, window)))

    def control(self, name: str = "c1", window: str = WINDOW, **kw) -> None:
        """name's viewer killed by hand marks it hidden: hooks run, so an earlier one has run."""
        self.kill_viewer(name, window)
        live_tmux.wait(lambda: self.hidden(name, **kw), what=f"{name} hidden")

    def test_killing_the_viewer_pane_marks_it_hidden(self):
        self.record(self.start("w1"))
        self.kill_viewer("w1")
        live_tmux.wait(self.hidden, what="w1 hidden")
        self.assertEqual(self.server.tmux("has-session", "-t", "=w1").returncode, 0)

    def test_hide_if_closed_changes_a_shown_worker_nothing(self):
        self.record(self.start("w1"))
        pane = self.shows()
        self.assertEqual(self.ok(self.session("hide", "--if-closed", "w1")).stdout, "")
        self.assertEqual((self.shows(), self.hidden()), (pane, False))
        self.assertEqual(self.session("hide", "--if-closed", "w9").returncode, 1)

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

    def clients(self, name: str = "w1") -> list[str]:
        found = self.server.tmux("list-clients", "-t", f"={name}", "-F", "#{client_tty}")
        return found.stdout.split()

    def test_detaching_the_viewer_or_then_another_client_marks_nothing(self):
        self.record(self.start("w1"))
        tty = self.server.tmux("display-message", "-p", "-t", self.shows(), "#{pane_tty}")
        tty = tty.stdout.strip()
        self.ok(self.server.tmux("detach-client", "-t", tty))
        live_tmux.wait(lambda: tty not in self.clients(), what="w1's viewer detached")
        # its @pane now names a gone pane: only its viewer's own detach may judge it closed
        self.server.attach("w1")
        self.ok(self.server.tmux("detach-client", "-s", "=w1"))
        live_tmux.wait(lambda: not self.clients(), what="w1's other client detached")
        self.record(self.start("c1"))
        self.control()
        self.assertFalse(self.hidden())

    def test_a_client_switched_in_after_the_viewer_detached_forgets_the_viewer(self):
        self.tmux("new-session", "-d", "-s", OTHER, "-x", "200", "-y", "50", *live_tmux.IDLE)
        self.server.attach(OTHER)
        (other,) = self.clients(OTHER)
        self.record(self.start("w1"))
        tty = self.server.tmux("display-message", "-p", "-t", self.shows(), "#{pane_tty}")
        tty = tty.stdout.strip()
        self.ok(self.server.tmux("detach-client", "-t", tty))
        live_tmux.wait(lambda: tty not in self.clients(), what="w1's viewer detached")
        self.tmux("switch-client", "-c", other, "-t", "=w1")
        live_tmux.wait(lambda: self.viewer() == "", what="w1's gone viewer forgotten")
        self.tmux("detach-client", "-s", "=w1")
        live_tmux.wait(lambda: not self.clients(), what="w1's switched client detached")
        self.record(self.start("c1"))
        self.control()
        self.assertFalse(self.hidden())

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
        for path in (home, bin_dir):
            os.mkdir(path)
        shutil.copy2(self.server.program, bin_dir)
        fake_tui.install(bin_dir)
        # hooks run with the server's environment: one HOME, as outside tests
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
        self.assertEqual((self.count(), self.viewer()), (before, ""))
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
