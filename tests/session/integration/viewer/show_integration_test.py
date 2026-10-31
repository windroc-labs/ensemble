"""Worker panes running the Host's attach command on a private tmux server."""

import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import uuid
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from ensemble.session import Viewer
from ensemble.session.engine.types import Launch, Liveness, SessionBusy, SessionError
from ensemble.session.hosts.base import worker_dir
from ensemble.session.hosts.docker.host import DockerHost
from ensemble.session.hosts.tmux import TmuxHost
from ensemble.session.viewer import display as displays
from ensemble.session.viewer.viewer import fold
from tests.session import guard_fixture, live_tmux
from tests.session.case import SID, Case
from tests.session.tmux_fixtures import close_hook, placed


class LiveCase(Case):
    def tmux(self, *args: str) -> str:
        res = self.server.tmux(*args)
        self.assertEqual(res.returncode, 0, f"tmux {args}: {res.stderr}")
        return res.stdout.removesuffix("\n")

    def display(self, pane: str, fmt: str) -> str:
        return self.tmux("display-message", "-p", "-t", pane, fmt)

    def started_by(self, pane: str) -> list[str]:
        return shlex.split(self.display(pane, "#{pane_start_command}"))

    def close_hooks(self, window: str) -> str:
        return self.tmux("show-hooks", "-w", "-t", window, "after-kill-pane")


class GuardDisplayTest(LiveCase):
    NAME = "color-worker"

    def setUp(self):
        self.server = live_tmux.Server(self, self.home)
        self.server.start("manager")
        self.server.attach("manager")
        self.enterContext(mock.patch.dict(os.environ, self.server.env(), clear=True))
        self.host, self.viewer = TmuxHost("tmux"), Viewer()
        self.launch = Launch(
            (sys.executable, "-I", guard_fixture.__file__, "colors", self.home),
            self.server.env(),
            cwd=self.home,
        )
        self.worker = self.host.worker(
            self.NAME,
            self.launch,
            terminal=True,
            display="manager",
            metadata={"generation": uuid.uuid4().hex, "sid": SID, "client": "fake"},
        )
        self.addCleanup(self.cleanup, self.worker)
        state = Path(worker_dir(self.NAME), "session.json")
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(
            json.dumps(
                {
                    "sid": SID,
                    "generation": self.worker.generation,
                    "client": "fake",
                    "display": "manager",
                }
            )
        )
        live_tmux.wait(
            lambda: "startup-079" in self.worker.screen(lines=200),
            what="startup output collected before the first display",
        )
        self.pid = int(Path(self.home, "display.pid").read_text())

    def cleanup(self, worker):
        with contextlib.suppress(SessionError):
            worker.close()

    def capture(self, pane: str) -> str:
        return self.tmux("capture-pane", "-p", "-e", "-S", "-", "-t", pane)

    def shown(self, name: str | None = None) -> str:
        """name's worker pane, shown from the manager's pane."""
        name = name or self.NAME
        with mock.patch.dict(os.environ, self.server.inside("manager")):
            self.assertIsNone(self.viewer.show(name, self.host, display="manager"))
        pane = self.server.pane_of(name)
        self.assertRegex(pane, r"\A%[0-9]+\Z")
        return pane

    def replayed(self, pane: str, *texts: str) -> None:
        live_tmux.wait(
            lambda: all(text in self.capture(pane) for text in texts),
            what=f"{texts!r} in {pane}'s scrollback",
        )

    def output(self, text: str) -> None:
        """NAME prints text (in green) with no display."""
        self.worker.write((text + "\n").encode())
        live_tmux.wait(lambda: text in self.worker.screen(lines=200), what=f"{text} printed")

    def hidden(self) -> bool:
        return displays.status(self.viewer, self.host, self.NAME, SID, None).hidden

    def assert_worker_unchanged(self):
        self.assertEqual(int(Path(self.home, "display.pid").read_text()), self.pid)
        self.assertTrue(live_tmux.alive(self.pid))
        self.assertTrue(live_tmux.alive(self.worker.proc.pid))
        self.assertEqual(self.worker.state(), Liveness.LIVE)
        self.assertIn(self.NAME, self.host.list())

    def test_pane_runs_the_attach_command_without_a_nested_client(self):
        """The pane runs CommandView.argv with no nested tmux client."""
        target = self.host.attach_target(self.NAME)
        pane = self.shown()
        self.assertEqual(self.started_by(pane), list(target.argv))
        self.assertIn(pane, self.server.panes("=manager:"))
        self.assertEqual(self.server.opener_of(self.NAME), "manager")
        self.replayed(pane, "\x1b[31mstartup-000")
        clients = self.tmux("list-clients", "-F", "#{session_name}").split()
        self.assertNotIn(self.NAME, clients)
        self.assertEqual(clients, ["manager"])
        self.assertNotEqual(self.server.tmux("has-session", "-t", f"={self.NAME}").returncode, 0)

    def test_start_show_hide_show_makes_no_tmux_session(self):
        """A worker shown, hidden and shown again never has a tmux session."""

        def no_session():
            rows = self.tmux("list-sessions", "-F", "#{session_name}\t#{@view}").splitlines()
            self.assertEqual(rows, ["manager\t"])

        no_session()
        self.shown()
        no_session()
        self.viewer.hide(self.NAME, self.host)
        self.assertEqual(self.server.pane_of(self.NAME), "")
        no_session()
        self.shown()
        no_session()

    def test_hide_keeps_the_guard_and_show_replays_hidden_output(self):
        """hide kills only its pane; a show replays its scrollback."""
        other = self.host.worker(
            "other",
            Launch(live_tmux.IDLE, self.server.env(), cwd=self.home),
            terminal=True,
            metadata={"generation": uuid.uuid4().hex},
        )
        self.addCleanup(self.cleanup, other)
        pane, beside = self.shown(), self.shown("other")
        guard = self.worker.proc.pid
        self.viewer.hide(self.NAME, self.host)
        panes = self.server.panes("=manager:")
        self.assertNotIn(pane, panes)
        self.assertIn(beside, panes)
        self.assertEqual(self.server.pane_of("other"), beside)
        self.assertEqual(self.server.pane_of(self.NAME), "")
        self.assertEqual(self.worker.proc.pid, guard)
        self.assert_worker_unchanged()
        self.assertEqual(sorted(self.host.list()), sorted([self.NAME, "other"]))
        self.assertTrue(self.hidden())
        self.output("hidden-output")
        shown = self.shown()
        self.assertNotEqual(shown, pane)
        self.replayed(shown, "\x1b[31mstartup-000", "\x1b[32mhidden-output")
        self.assertFalse(self.hidden())
        self.assert_worker_unchanged()

    def test_closing_the_pane_keeps_the_guard_inventory_and_show_reopens(self):
        """A worker pane killed by hand ends no worker; a show opens a new pane."""
        target = self.host.attach_target(self.NAME)
        pane = self.shown()
        self.tmux("kill-pane", "-t", pane)
        self.assertEqual(self.server.pane_of(self.NAME), "")
        self.assert_worker_unchanged()
        with self.assertRaises(SessionBusy):
            self.host.worker(
                self.NAME, self.launch, terminal=True, metadata={"generation": uuid.uuid4().hex}
            )
        self.output("manual-output")
        shown = self.shown()
        self.assertNotEqual(shown, pane)
        self.replayed(shown, "\x1b[31mstartup-000", "\x1b[32mmanual-output")
        self.assertEqual(self.host.attach_target(self.NAME), target)
        self.assertEqual(self.started_by(shown), list(target.argv))
        self.assert_worker_unchanged()

    def test_worker_exit_closes_its_pane_and_leaves_no_session(self):
        """A worker's exit closes its pane; the next kill-pane removes its close hook."""
        pane = self.shown()
        window = self.display(pane, "#{window_id}")
        manager = self.server.inside("manager")["TMUX_PANE"]
        hook = f"after-kill-pane[{pane[1:]}]"
        self.assertIn(hook, self.close_hooks(window))
        self.worker.close()
        live_tmux.wait(
            lambda: not self.server.worker_panes(self.NAME, dead=True),
            what=f"{self.NAME}'s worker pane closed",
        )
        self.assertEqual(set(self.server.panes(window)), {manager})
        self.assertEqual(self.server.sessions(), ["manager"])
        self.assertNotIn(self.NAME, self.host.list())
        self.assertIn(hook, self.close_hooks(window))
        manual = self.tmux(
            "split-window", "-d", "-P", "-F", "#{pane_id}", "-t", manager, *live_tmux.IDLE
        )
        self.tmux("kill-pane", "-t", manual)
        live_tmux.wait(
            lambda: hook not in self.close_hooks(window), what=f"{hook} removed by its own run"
        )

    def old_view(self) -> str:
        """NAME's old view session; its nested pane."""
        manager = self.server.inside("manager")["TMUX_PANE"]
        self.tmux(
            "new-session", "-d", "-s", self.NAME, "--", *self.host.attach_target(self.NAME).argv
        )
        nested = ("/usr/bin/env", "-u", "TMUX", self.server.program, "-S", self.server.socket)
        pane = self.tmux(
            *("split-window", "-d", "-h", "-P", "-F", "#{pane_id}", "-t", manager),
            *(*nested, "attach", "-t", f"={self.NAME}"),
        )
        live_tmux.wait(
            lambda: self.NAME in self.tmux("list-clients", "-F", "#{session_name}").split(),
            what=f"the nested client attached to {self.NAME}'s view session",
        )
        target = f"={self.NAME}:"
        ran = f"run-shell {shlex.quote('touch ' + shlex.quote(str(self.old_hook)))}"
        self.tmux(
            *("set-option", "-t", target, "@view", "viewer", ";"),
            *("set-option", "-t", target, "@pane", pane, ";"),
            *("set-option", "-t", target, "@opener", "manager", ";"),
            *("set-option", "-w", "-t", target, "remain-on-exit", "on", ";"),
            *("set-hook", "-t", target, "client-attached", ran, ";"),
            *("set-hook", "-t", target, "client-session-changed", ran, ";"),
            *("set-hook", "-t", target, "client-detached", ran),
        )
        return pane

    @property
    def old_hook(self) -> Path:
        return Path(self.home, "old-hook")

    def assert_old_view_gone(self, old: str) -> None:
        self.assertNotIn(old, self.server.panes("=manager:"))
        self.assertEqual(self.server.sessions(), ["manager"])
        self.assertFalse(self.old_hook.exists())
        self.assert_worker_unchanged()

    def test_show_replaces_an_old_view_session_with_a_direct_pane(self):
        """One direct pane; the worker runs on."""
        old = self.old_view()
        manager = self.server.inside("manager")["TMUX_PANE"]
        pane = self.shown()
        self.assert_old_view_gone(old)
        self.assertEqual([w.pane for w in self.server.worker_panes(self.NAME)], [pane])
        self.assertEqual(set(self.server.panes("=manager:")), {manager, pane})
        self.assertEqual(self.started_by(pane), list(self.host.attach_target(self.NAME).argv))
        self.replayed(pane, "startup-079")
        self.assertFalse(self.hidden())
        self.assertEqual(self.shown(), pane)

    def test_hide_or_close_removes_an_old_view_session(self):
        """hide and close too."""
        for verb in ("hide", "close"):
            with self.subTest(verb=verb):
                old = self.old_view()
                getattr(self.viewer, verb)(self.NAME, self.host)
                self.assert_old_view_gone(old)
                self.assertTrue(self.hidden())
        pane = self.shown()
        self.assertEqual(self.started_by(pane), list(self.host.attach_target(self.NAME).argv))
        self.assert_worker_unchanged()


class ShowTest(LiveCase):
    def setUp(self):
        self.server = live_tmux.Server(self, self.home)
        self.server.start("mgr")
        self.server.attach("mgr")
        self.enterContext(mock.patch.dict(os.environ, self.server.env(), clear=True))
        self.host = TmuxHost("tmux")
        self.worker = self.host.worker(
            "worker",
            Launch(live_tmux.IDLE, self.server.env(), cwd=self.home),
            terminal=True,
            metadata={"generation": uuid.uuid4().hex},
        )
        self.addCleanup(self.worker.close)
        self.target = self.host.attach_target("worker")
        self.manager = self.server.inside("mgr")["TMUX_PANE"]
        self.calls = []
        self.fail_metadata = False
        self.server.tmux("set-option", "-w", "-t", "=mgr:", "remain-on-exit", "on")

    def transport(self, argv, **kwargs):
        self.calls.append(argv)
        if self.fail_metadata and argv[1:3] == ["set-option", "-p"]:
            return subprocess.CompletedProcess(argv, 1, "", "metadata refused")
        return subprocess.run(argv, **kwargs)

    def open(self):
        """open_pane outside tmux, below mgr's shown pane."""
        self.stderr = io.StringIO()
        with redirect_stderr(self.stderr):
            return placed(self.transport).open_pane(
                "worker",
                self.target.argv,
                self.target.printed,
                sid=SID,
                generation=self.worker.generation,
                split="below",
                split_from="mgr",
            )

    def split(self) -> list[str]:
        return next(argv for argv in self.calls if argv[1:2] == ["split-window"])

    def added(self, before) -> str:
        after = set(self.server.panes("=mgr:"))
        self.assertLess(set(before), after)
        (new,) = after - set(before)
        return new

    def test_split_from_runs_the_attach_command_and_tags_the_pane(self):
        """--split-from mgr splits mgr's shown pane, tagged with its close hook."""
        before = self.server.panes("=mgr:")
        self.assertIsNone(self.open())
        new = self.added(before)
        self.assertEqual(
            self.split(),
            [
                *("tmux", "split-window", "-d", "-v", "-P", "-F", "#{pane_id}\t#{window_id}"),
                *("-t", self.manager, "--", *self.target.argv),
            ],
        )
        self.assertEqual(self.started_by(new), list(self.target.argv))
        self.assertEqual(self.server.pane_of("worker"), new)
        self.assertEqual(self.server.opener_of("worker"), "")
        hooks = self.close_hooks(self.display(new, "#{window_id}"))
        self.assertIn(f"after-kill-pane[{new[1:]}]", hooks)
        run = f"--sid {SID} --generation {self.worker.generation}"
        self.assertIn(f" hide --if-closed {run} -- worker ", hooks)

    def test_a_users_after_kill_pane_entries_survive_show_close_and_prune(self):
        window = self.display(self.manager, "#{window_id}")
        mine = ("set-hook", "-a", "-w", "-t", window, "after-kill-pane")
        self.tmux(*mine, "display-message mine")
        stale = close_hook("worker", "%900", window, index=900)
        self.tmux("set-hook", "-w", "-t", window, "after-kill-pane[900]", stale)
        before = self.server.panes("=mgr:")
        self.assertIsNone(self.open())
        new = self.added(before)
        hooks = self.close_hooks(window)
        self.assertNotIn("after-kill-pane[900]", hooks)
        self.assertIn(" display-message mine", hooks)
        self.assertIn(f"#{{m:* {new} *,", hooks)
        self.tmux(*mine, "display-message later")
        Viewer().hide("worker", self.host)
        self.assertNotIn(new, self.server.panes("=mgr:"))
        hooks = self.close_hooks(window)
        self.assertNotIn(" hide --if-closed ", hooks)
        for entry in (" display-message mine", " display-message later"):
            self.assertIn(entry, hooks)

    def test_split_right_runs_the_attach_command(self):
        """--split right with --split-from mgr and no grid opens right of mgr's pane."""
        self.tmux("new-session", "-d", "-s", "side", *live_tmux.IDLE)
        before = self.server.panes("=mgr:")
        with mock.patch.dict(os.environ, self.server.inside("side")):
            self.assertIsNone(
                Viewer().show("worker", self.host, display=fold(None, "right", "mgr"))
            )
        new = self.added(before)
        self.assertEqual(self.server.pane_of("worker"), new)
        x, y, _, h = before[self.manager]
        after = self.server.panes("=mgr:")
        self.assertEqual((after[new][0], after[new][1]), (x + after[self.manager][2] + 1, y))
        self.assertEqual(after[new][3], h)
        self.assertEqual(self.started_by(new), list(self.target.argv))
        self.assertEqual(self.server.opener_of("worker"), "side")
        self.assertEqual(self.server.sessions(), ["mgr", "side"])

    def test_metadata_failure_leaves_the_successful_split_and_unrelated_parent_alive(self):
        self.fail_metadata = True
        before = self.server.panes("=mgr:")
        self.assertIsNone(self.open())
        new = self.added(before)
        self.assertEqual(self.started_by(new), list(self.target.argv))
        self.assertEqual(self.server.pane_of("worker"), "")
        self.assertNotIn(f"after-kill-pane[{new[1:]}]", self.close_hooks("=mgr:"))
        self.assertIn("tui: show: @worker: tmux: metadata refused", self.stderr.getvalue())
        self.assertEqual(self.server.tmux("has-session", "-t", "=mgr").returncode, 0)

    def test_a_command_runs_in_place_of_a_pane_with_the_session_name(self):
        shown = os.path.join(self.home, "shown")
        before = set(self.server.panes("=mgr:"))
        command = f"printf %s {{{{session}}}} > {shown}"
        self.assertIsNone(Viewer().show("worker", self.host, command=command))
        with open(shown) as f:
            self.assertEqual(f.read(), "worker")
        self.assertEqual(set(self.server.panes("=mgr:")), before)
        self.assertEqual(self.server.pane_of("worker"), "")


class LabelTest(LiveCase):
    """A /bin/sh worker w1, shown from mgr's pane."""

    def setUp(self):
        self.server = live_tmux.Server(self, self.home)
        self.server.start("mgr")
        self.server.attach("mgr")
        self.enterContext(mock.patch.dict(os.environ, self.server.env(), clear=True))
        self.host = TmuxHost("tmux")
        self.worker = self.host.worker(
            "w1",
            Launch(("/bin/sh",), self.server.env(), cwd=self.home),
            terminal=True,
            metadata={"generation": uuid.uuid4().hex},
        )
        self.addCleanup(self.worker.close)

    def show(self, host) -> str:
        with mock.patch.dict(os.environ, self.server.inside("mgr")):
            self.assertIsNone(Viewer().show("w1", host))
        pane = self.server.pane_of("w1")
        self.assertRegex(pane, r"\A%[0-9]+\Z")
        return pane

    def sized(self, pane: str) -> None:
        """Waits until w1's terminal has pane's size."""
        live_tmux.sized(
            self.worker,
            lambda: self.display(pane, "#{pane_width}x#{pane_height}"),
            f"w1's terminal, shown in {pane},",
        )

    def test_a_docker_tui_pane_shows_its_name_in_the_border(self):
        """A Docker worker pane's border shows [🐳 NAME]; its terminal keeps every row."""
        before = self.tmux("show-options", "-g")
        manager = self.tmux("show-options", "-t", "=mgr:")
        host = DockerHost("docker", {"docker": "/usr/local/bin/docker"})
        with mock.patch.object(host, "attach_target", return_value=self.host.attach_target("w1")):
            pane = self.show(host)
        window = self.display(pane, "#{window_id}")
        self.assertEqual(
            self.tmux("show-options", "-wqv", "-t", window, "pane-border-status"), "bottom"
        )
        self.assertEqual(self.display(pane, "#{@worker-label}"), "w1")
        self.assertEqual(self.display(pane, "#{T:pane-border-format}"), "[🐳 w1]")
        self.sized(pane)
        self.assertEqual(self.tmux("show-options", "-g"), before)
        self.assertEqual(self.tmux("show-options", "-t", "=mgr:"), manager)

    def test_hiding_the_last_labelled_pane_gives_the_window_its_own_borders_back(self):
        """mgr's own pane-border-status top, format unset, as they were after a hide."""
        self.tmux("set-option", "-w", "-t", "=mgr:", "pane-border-status", "top")
        options = (
            *("pane-border-status", "pane-border-format"),
            *("@worker-saved-pane-border-status", "@worker-saved-pane-border-format"),
        )
        mine = [self.tmux("show-options", "-wq", "-t", "=mgr:", option) for option in options]
        self.assertEqual(mine, ["pane-border-status top", "", "", ""])
        host = TmuxHost("tmux", {"status_line": True})
        pane = self.show(host)
        self.assertEqual(
            self.display(pane, "#{T:pane-border-format}|#{pane-border-status}"), "[w1]|bottom"
        )
        with mock.patch.dict(os.environ, self.server.inside("mgr")):
            Viewer().hide("w1", host)
        self.assertNotIn(pane, self.server.panes("=mgr:"))
        after = [self.tmux("show-options", "-wq", "-t", "=mgr:", option) for option in options]
        self.assertEqual(after, mine)

    def test_tmux_host_pane_has_no_label_by_default(self):
        """status_line false (the default): no border status, no label."""
        pane = self.show(self.host)
        window = self.display(pane, "#{window_id}")
        self.assertEqual(self.display(pane, "#{pane-border-status}|#{@worker-label}"), "off|")
        for option in ("pane-border-status", "pane-border-format"):
            self.assertEqual(self.tmux("show-options", "-wqv", "-t", window, option), "")
        self.assertEqual(
            self.display(pane, "#{pane_height}"), self.display(pane, "#{window_height}")
        )
        self.sized(pane)
