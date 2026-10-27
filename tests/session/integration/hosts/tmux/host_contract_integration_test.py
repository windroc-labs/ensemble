"""HostContract (FR-5) against TmuxHost on a private tmux server (tests/session/live_tmux.py),
os.environ that server's env; the terminal cases wait for TmuxHost.terminal, so their
detach halves run here. Also: a forged @record, a display completed once and stored, and a
session the user split and refocused, read through its launched pane.
"""

import os
import signal
import sys
import unittest
from pathlib import Path
from unittest import mock

from ensemble.session import SessionError, Viewer
from ensemble.session.hosts.tmux import TmuxHost
from ensemble.session.viewer import display as displays
from ensemble.utils import StatusState
from tests.session import live_tmux
from tests.session.case import Case
from tests.session.contracts.host import (
    EXIT_7,
    SLEEP,
    HostContract,
    gone,
    read,
)

MANAGER = "mgr"
USER = "print('user pane', flush=True); import time; time.sleep(60)"
GATED = (
    sys.executable,
    "-I",
    "-c",
    "import os, sys, time\n"
    "print('agent pane', flush=True)\n"
    "while not os.path.exists(sys.argv[1]): time.sleep(0.01)\n"
    "sys.exit(7)\n",
)


class TmuxHostContractTest(HostContract, Case):
    def setUp(self):
        self.server = live_tmux.Server(self, self.home)
        self.server.start(MANAGER)
        colored = self.server.tmux("set-environment", "-g", "COLORTERM", "truecolor")
        self.assertEqual(colored.returncode, 0, colored.stderr)
        patch = mock.patch.dict(os.environ, self.server.env(), clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        super().setUp()

    def creator_env(self):
        return {**super().creator_env(), "TMUX_TMPDIR": self.server.root}

    def make_host(self):
        return TmuxHost("tmux")

    def poll(self, check, what: str):
        def poked():
            # tmux on Linux (CI: 3.4) can leave a dead pane's process unreaped, so no pane-died,
            # until another child of the server exits: a no-op job is one
            self.server.tmux("run-shell", "-b", "true")
            return check()

        return super().poll(poked, what)

    def assert_terminal_identity(self, env):
        terminal = self.server.tmux("show-options", "-gv", "default-terminal")
        self.assertEqual(terminal.returncode, 0, terminal.stderr)
        self.assertEqual(env["TERM"], terminal.stdout.strip())
        self.assertEqual(env["TMUX"].split(",")[0], self.server.socket)
        self.assertRegex(env["TMUX_PANE"], r"\A%[0-9]+\Z")

    def split(self, name: str) -> str:
        """A pane of the user's split into name's window, focused once it prints: its id."""
        added = self.server.tmux(
            "split-window",
            "-P",
            "-F",
            "#{pane_id}",
            "-t",
            f"={name}:",
            sys.executable,
            "-I",
            "-c",
            USER,
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        other = added.stdout.strip()
        active = self.server.tmux("display-message", "-p", "-t", f"={name}:", "#{pane_id}")
        self.assertEqual(active.stdout.strip(), other)
        self.poll(
            lambda: "user pane" in self.server.tmux("capture-pane", "-p", "-t", other).stdout,
            "the user's pane printing",
        )
        return other

    def row(self, name: str):
        return next(row for row in self.host.rows() if row.name == name)

    def test_a_split_refocused_terminal_is_listed_and_read_through_its_launched_pane(self):
        name, gate = self.fresh(), self.file("gate")
        self.start("terminal", name, GATED, gate)
        self.poll(
            lambda: (
                "agent pane" in self.server.tmux("capture-pane", "-p", "-t", f"={name}:").stdout
            ),
            "the launched pane printing",
        )
        self.split(name)
        self.assertEqual(self.row(name).state, StatusState.WORKING)
        for screen in (self.host.attach(name).screen(), self.host.attach(name).screen(50)):
            self.assertIn("agent pane", screen)
            self.assertNotIn("user pane", screen)
        Path(gate).touch()
        self.poll(lambda: self.row(name).state == StatusState.DEAD, "the launched pane dead")
        last = self.row(name).last_lines
        self.assertIn("agent pane", last)
        self.assertNotIn("user pane", last)
        self.assertNotIn("user pane", self.host.attach(name).screen())

    def test_a_split_refocused_detached_sessions_exit_closes_only_its_launched_pane(self):
        name, gate, events = self.fresh(), self.file("gate"), self.file("events")
        self.start("detach", name, GATED, gate, events=events)
        self.poll(
            lambda: (
                "agent pane" in self.server.tmux("capture-pane", "-p", "-t", f"={name}:").stdout
            ),
            "the launched pane printing",
        )
        other = self.split(name)
        Path(gate).touch()
        self.event(events, name, "exited 7")

        def panes():
            listed = self.server.tmux("list-panes", "-s", "-t", f"={name}:", "-F", "#{pane_id}")
            self.assertEqual(listed.returncode, 0, listed.stderr)
            return listed.stdout.split()

        self.poll(lambda: panes() == [other], "only the user's pane left")
        self.assertIn(name, self.host.list())
        self.assertEqual(self.row(name).state, StatusState.DEAD)

    def test_list_without_a_display_names_every_detached_session_sorted(self):
        first, second = self.fresh(), self.fresh()
        self.start("detach", second, SLEEP)
        self.start("detach", first, SLEEP, display="elsewhere:1.2")
        self.assertEqual(self.host.list(), sorted([MANAGER, first, second]))

    def test_the_exited_hook_then_kills_the_session_it_kept(self):
        name, events = self.fresh(), self.file("events")
        self.start("detach", name, EXIT_7, events=events)
        self.event(events, name, "exited 7")
        self.poll(lambda: name not in self.host.list(), f"{name} gone")

    def test_killing_the_pane_stops_its_program_and_keeps_ownership_of_leftover_children(self):
        name, ready, stopping, finish, finished = (
            self.fresh(),
            self.file("ready"),
            self.file("stopping"),
            self.file("finish"),
            self.file("finished"),
        )
        script = """import os, signal, sys, time
ready, stopping, finish, finished = sys.argv[1:]
signal.signal(signal.SIGHUP, signal.SIG_IGN)
def stop(signum, frame):
    with open(stopping, 'a') as stream: stream.write('term\\n')
    while not os.path.exists(finish): time.sleep(0.01)
    with open(finished, 'w') as stream: stream.write('cleanup complete')
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
signal.pthread_sigmask(signal.SIG_UNBLOCK, (signal.SIGHUP, signal.SIGTERM, signal.SIGINT))
child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with open(ready + '.child', 'w'): pass
    while True: time.sleep(1)
while not os.path.exists(ready + '.child'): time.sleep(0.01)
with open(ready, 'w') as stream: stream.write(f'{os.getpid()} {child}')
while True: time.sleep(1)
"""
        process = self.start(
            "detach", name, (sys.executable, "-I", "-c", script), ready, stopping, finish, finished
        )
        self.addCleanup(process.close)
        own, child = map(int, self.poll(lambda: read(ready), "program and child ready").split())
        control = self.server.option(name, "@control")
        self.assertTrue(os.path.isdir(control))
        pane = self.server.tmux("display-message", "-p", "-t", f"={name}:", "#{pane_pid}")
        self.assertEqual(pane.returncode, 0, pane.stderr)
        pane_pid = int(pane.stdout.strip())
        self.assertNotIn(pane_pid, (own, child))
        os.kill(pane_pid, signal.SIGKILL)
        self.poll(lambda: name not in self.host.list(), "killed pane session gone")
        self.poll(lambda: read(stopping), "program SIGTERM cleanup")
        self.assertFalse(process.wait_exit(0.2))
        self.assertFalse(gone(own))
        self.assertIsNone(read(finished))
        Path(finish).touch()
        self.poll(lambda: read(finished) == "cleanup complete", "program cleanup complete")
        self.poll(lambda: gone(own), "program exit")
        self.assertEqual(read(stopping), "term\n")
        self.assertFalse(gone(child))
        self.assertFalse(process.wait_exit(0.2))
        self.assertTrue(os.path.isdir(control))
        process.close()
        self.poll(lambda: gone(child), "leftover child exit")
        self.poll(lambda: not os.path.exists(control), "controller generation retired")
        self.assertTrue(process.wait_exit(0))

    def test_close_twice_of_a_detached_session_is_fine_and_forgets_it(self):
        name = self.fresh()
        process = self.start("detach", name, SLEEP)
        process.close()
        process.close()
        self.assertFalse(process.alive())
        self.assertNotIn(name, self.host.list())
        with self.assertRaises(SessionError):
            self.host.attach(name)

    def test_a_forged_record_is_none(self):
        name = self.fresh()
        self.start("detach", name, SLEEP, record=self.a_record(name))
        for forged in ('{"v":1}', "{not json"):
            with self.subTest(forged=forged):
                self.server.tmux("set-option", "-t", f"={name}:", "@record", forged)
                self.assertIsNone(self.host.attach(name).record())

    def test_a_display_completed_once_is_stored_as_given_and_listed_by_the_viewer(self):
        viewer = Viewer()
        with mock.patch.dict(os.environ, self.server.inside(MANAGER)):
            display = displays.display(viewer, "/right", self.host)
        self.assertEqual(display, f"{MANAGER}/right")
        name = self.fresh()
        self.start("detach", name, SLEEP, display=display)
        self.assertEqual(self.host.attach(name).display(), display)
        self.assertEqual(self.server.option(name, "@opener"), "")
        self.assertIn(name, displays.names(viewer, self.host, display))


if __name__ == "__main__":
    unittest.main()
