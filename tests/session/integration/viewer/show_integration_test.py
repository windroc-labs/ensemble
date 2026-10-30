"""Display placement against a private server, including partial-success recovery."""

import io
import os
import subprocess
import sys
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from ensemble.session import Viewer
from ensemble.session.engine.types import Launch, Liveness, SessionBusy
from ensemble.session.hosts.docker.host import DockerHost
from ensemble.session.hosts.tmux import TmuxHost
from ensemble.session.hosts.types import CommandView
from tests.session import guard_fixture, live_tmux
from tests.session.case import SID, Case
from tests.session.tmux_fixtures import placed


class GuardDisplayTest(Case):
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
            metadata={"sid": SID, "client": "fake"},
        )
        self.addCleanup(self.cleanup_worker)
        live_tmux.wait(
            lambda: "startup-079" in self.worker.screen(lines=200),
            what="startup output collected before the first display",
        )
        self.pid = int(Path(self.home, "display.pid").read_text())

    def cleanup_worker(self):
        try:
            self.worker.close()
        finally:
            self.viewer.close(self.NAME, self.host)

    def capture(self):
        result = self.server.tmux("capture-pane", "-p", "-e", "-S", "-", "-t", f"={self.NAME}:")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def shown(self):
        with mock.patch.dict(os.environ, self.server.inside("manager")):
            self.assertIsNone(self.viewer.show(self.NAME, self.host, display="manager"))

    def output(self, text):
        self.worker.write((text + "\n").encode())
        live_tmux.wait(lambda: f"\x1b[32m{text}" in self.capture(), what=f"colored {text}")

    def assert_worker_unchanged(self):
        self.assertEqual(int(Path(self.home, "display.pid").read_text()), self.pid)
        self.assertTrue(live_tmux.alive(self.pid))
        self.assertEqual(self.worker.state(), Liveness.LIVE)
        self.assertEqual(self.host.list(), [self.NAME])

    def test_startup_hide_and_manual_pane_close_preserve_colored_scrollback(self):
        self.assertTrue(self.viewer._prepare(self.NAME, self.host))
        self.assertEqual(self.server.option(self.NAME, "@view"), "viewer")
        self.assertEqual(self.server.option(self.NAME, "@pane"), "")
        live_tmux.wait(
            lambda: "\x1b[31mstartup-000" in self.capture(),
            what="colored history before first show",
        )
        inner = self.server.tmux(
            "display-message", "-p", "-t", f"={self.NAME}:", "#{pane_id}"
        ).stdout
        self.shown()
        self.viewer.hide(self.NAME, self.host)
        self.output("hidden-output")
        self.shown()
        outer = self.server.option(self.NAME, "@pane")
        killed = self.server.tmux("kill-pane", "-t", outer)
        self.assertEqual(killed.returncode, 0, killed.stderr)
        self.output("manual-output")
        self.shown()
        self.assertIn("\x1b[31mstartup-000", self.capture())
        self.assertIn("\x1b[32mhidden-output", self.capture())
        self.assertEqual(
            self.server.tmux("display-message", "-p", "-t", f"={self.NAME}:", "#{pane_id}").stdout,
            inner,
        )
        self.assert_worker_unchanged()

    def test_deleting_display_keeps_guard_inventory_and_show_rebuilds(self):
        target = self.host.attach_target(self.NAME)
        self.viewer._prepare(self.NAME, self.host)
        killed = self.server.tmux("kill-session", "-t", f"={self.NAME}")
        self.assertEqual(killed.returncode, 0, killed.stderr)
        self.assert_worker_unchanged()
        with self.assertRaises(SessionBusy):
            self.host.worker(self.NAME, self.launch, terminal=True)
        self.shown()
        self.output("rebuilt-output")
        self.assertEqual(self.host.attach_target(self.NAME), target)
        self.assert_worker_unchanged()


class ShowTest(Case):
    def setUp(self):
        self.server = live_tmux.Server(self, self.home)
        self.server.start("mgr")
        self.server.attach("mgr")
        self.enterContext(mock.patch.dict(os.environ, self.server.env(), clear=True))
        self.host = TmuxHost("tmux")
        self.worker = self.host.worker(
            "worker", Launch(live_tmux.IDLE, self.server.env(), cwd=self.home), terminal=True
        )
        self.addCleanup(self.worker.close)
        Viewer()._prepare("worker", self.host)
        self.environment = self.server.env(**self.server.inside("mgr"))
        self.calls = []
        self.fail_metadata = False
        self.new_pane = None
        self.server.tmux("set-option", "-w", "-t", "=mgr:", "remain-on-exit", "on")

    def transport(self, argv, **kwargs):
        self.calls.append(argv)
        if self.fail_metadata and argv[1:4] == ["set-option", "-t", "=worker:"]:
            return subprocess.CompletedProcess(argv, 1, "", "metadata refused")
        response = subprocess.run(argv, **kwargs)
        if argv[:2] == ["tmux", "split-window"]:
            self.new_pane = response.stdout.strip()
        return response

    def open(self):
        self.stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, self.environment, clear=True),
            redirect_stderr(self.stderr),
        ):
            return placed(self.transport).open_pane(
                "worker", split="below", split_from="mgr", opener="mgr"
            )

    def assert_visible_private_attach(self):
        split = next(argv for argv in self.calls if argv[:2] == ["tmux", "split-window"])
        try:
            live_tmux.wait(
                lambda: bool(self.server.tmux("list-clients", "-t", "=worker").stdout.strip()),
                what="the nested display attaches to the worker on the private server",
            )
        except AssertionError as failure:
            captured = self.server.tmux("capture-pane", "-p", "-t", self.new_pane)
            self.fail(
                f"{failure}\n{split!r}\n{captured.stdout}\n{captured.stderr}\n{self.server.tmux('list-sessions').stdout}"
            )
        executable = split[split.index("TMUX") + 1]
        self.assertTrue(os.path.isabs(executable))
        self.assertEqual(split[split.index("-S") + 1], self.server.socket)
        self.assertIn(
            ["/usr/bin/env", "-u", "TMUX"], [split[i : i + 3] for i in range(len(split) - 2)]
        )
        self.assertEqual(split[-3:], ["attach", "-t", "=worker"])
        self.assertEqual(self.server.tmux("has-session", "-t", "=mgr").returncode, 0)

    def test_nested_display_uses_the_private_socket_with_restricted_path(self):
        self.environment["PATH"] = self.server.bin
        self.assertIsNone(self.open())
        self.assert_visible_private_attach()
        self.assertEqual(self.server.option("worker", "@opener"), "mgr")
        self.assertRegex(self.server.option("worker", "@pane"), r"^%[0-9]+$")

    def test_metadata_failure_leaves_the_successful_split_and_unrelated_parent_alive(self):
        self.fail_metadata = True
        before = set(self.server.panes("=mgr:"))
        self.assertIsNone(self.open())
        self.assert_visible_private_attach()
        after = set(self.server.panes("=mgr:"))
        self.assertLess(before, after)
        self.assertEqual(len(after - before), 1)
        self.assertEqual(self.server.option("worker", "@pane"), "")
        self.assertIn("metadata refused", self.stderr.getvalue())

    def test_a_command_runs_in_place_of_a_pane_with_the_session_name(self):
        shown = os.path.join(self.home, "shown")
        before = set(self.server.panes("=mgr:"))
        command = f"printf %s {{{{session}}}} > {shown}"
        self.assertIsNone(Viewer().show("worker", self.host, command=command))
        with open(shown) as f:
            self.assertEqual(f.read(), "worker")
        self.assertEqual(set(self.server.panes("=mgr:")), before)
        self.assertEqual(self.server.option("worker", "@pane"), "")

    def test_a_docker_tui_view_shows_the_tmux_status_line_by_default(self):
        name = "engineer-WDRC-281-502d679e"
        before = self.server.tmux("show-options", "-g").stdout
        manager = self.server.tmux("show-options", "-t", "=mgr:").stdout
        host = DockerHost("docker", {"docker": "/usr/local/bin/docker"})
        with mock.patch.object(
            host, "attach_target", return_value=CommandView("cat -", live_tmux.IDLE)
        ):
            self.assertIsNone(Viewer().show(name, host, command="true"))
        status = self.server.tmux("show", "-t", f"={name}:", "status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(status.stdout.strip(), "status on")
        self.assertEqual(self.server.option(name, "status-left-length"), str(len(name) + 3))
        label = self.server.tmux("display-message", "-p", "-t", f"={name}:", "#{E:status-left}")
        self.assertEqual(label.returncode, 0, label.stderr)
        self.assertEqual(label.stdout, f"[{name}] \n")
        self.assertEqual(self.server.tmux("show-options", "-g").stdout, before)
        self.assertEqual(self.server.tmux("show-options", "-t", "=mgr:").stdout, manager)
