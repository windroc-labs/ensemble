"""Tmux Host fixtures: recorded tmux calls and controller responses, with owned state."""

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from ensemble.session.engine.tmux_cli import TmuxCli
from ensemble.session.engine.types import PaneStatus
from ensemble.session.hosts.tmux import TmuxHost
from ensemble.session.viewer.viewer import Viewer
from ensemble.utils import ProcessControl, Record
from tests.session.case import Case
from tests.session.tmux_fixtures import FakeTmux, which

LINE = Record(
    1,
    "role",
    "r1",
    ("r1",),
    None,
    "/w",
    ("ensemble-delegate",),
    ("ensemble-delegate", "resume", "/w"),
    None,
    None,
)
PROCESS = "4242 $3 %7 4343"


def fake_tmux(runner: FakeTmux) -> mock.Mock:
    """Recorded session verbs; run, checked and runner (Grid's, Placement's, Iterm's) play runner."""
    tmux = mock.create_autospec(TmuxCli, instance=True)
    tmux.PAUSE = TmuxCli.PAUSE
    tmux.runner = runner
    tmux.run.side_effect = TmuxCli(runner).run
    tmux.checked.side_effect = TmuxCli(runner).checked
    tmux.sessions.side_effect = TmuxCli(runner).sessions
    tmux.status.return_value = PaneStatus.RUNNING
    tmux.listing.return_value = []
    tmux.option.return_value = None
    tmux.detach.return_value = tmux.process.return_value = PROCESS
    return tmux


class Base(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.tool = os.path.join(self.dir, "tool")
        Path(self.tool).touch(mode=0o700)
        self.enterContext(which(self.tool))
        self.runner = FakeTmux()
        self.tmux = fake_tmux(self.runner)
        self.host = TmuxHost("tmux", tmux=self.tmux)
        self.viewer = Viewer({"workers_per_column": 2}, tmux=self.tmux)
        self.killpg = self.enterContext(
            mock.patch("os.killpg", side_effect=AssertionError("unsafe signal"))
        )
        self.control = self.controller()
        self.addCleanup(self.control.remove)
        self.completed(self.control.path)
        self.controlled = True
        self.kind = "detach"
        self.recorded = PROCESS
        self.tmux.option.side_effect = lambda _pane, key: (
            self.control.path
            if key == "@control" and self.controlled
            else self.kind
            if key == "@kind"
            else self.recorded
            if key == "@process"
            else self.tmux.option.return_value
        )
        self.tmux.detach.side_effect = self.detached
        self.tmux.start.side_effect = self.terminal_started
        self.socket = self.enterContext(mock.patch("socket.socket"))
        self.connection = self.socket.return_value.__enter__.return_value
        self.connection.makefile.return_value.__enter__.return_value.readline.return_value = (
            b'{"live": true}\n'
        )
        allocate = tempfile.mkdtemp

        def private(*args, **kw):
            if args:
                return allocate(*args, **kw)
            self.assertEqual(
                kw,
                {"prefix": "apm-group-", "dir": os.environ.get("ENSEMBLE_CONTROL_ROOT") or "/tmp"},
            )
            return allocate(prefix=kw["prefix"], dir=self.dir)

        self.enterContext(mock.patch("tempfile.mkdtemp", side_effect=private))

    def controller(self):
        path = os.path.join(self.dir, "controller")
        os.mkdir(path, 0o700)
        return ProcessControl(path)

    @staticmethod
    def completed(path):
        Path(path, "state.json").write_text(json.dumps({"started": {"started": True}, "done": 0}))

    def detached(self, *_args, **kw):
        self.control = ProcessControl(kw["control"])
        self.addCleanup(self.control.remove)
        Path(self.control.path, "state.json").write_text('{"started": {"started": true}}')
        return PROCESS

    def terminal_started(self, *_args, **kw):
        self.kind = "terminal"
        return self.detached(control=kw["tags"]["@control"])

    def active(self, *responses):
        self.controlled = True
        Path(self.control.path, "state.json").write_text('{"started": {"started": true}}')
        stream = self.connection.makefile.return_value.__enter__.return_value
        stream.readline.side_effect = [
            json.dumps(response).encode() + b"\n" for response in responses
        ]
