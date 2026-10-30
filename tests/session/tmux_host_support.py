"""Tmux Host fixtures: recorded tmux calls and controller responses, with owned state."""

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from ensemble.session.engine.tmux_cli import TmuxCli
from ensemble.session.hosts.tmux import TmuxHost
from ensemble.session.viewer.viewer import Viewer
from ensemble.utils import ProcessControl
from tests.session.case import Case
from tests.session.tmux_fixtures import FakeTmux, which
from tests.utils.sandbox import scratch


def fake_tmux(runner: FakeTmux) -> mock.Mock:
    """run, checked and runner (Grid's, Placement's) play runner; no tmux session is named."""
    tmux = mock.create_autospec(TmuxCli, instance=True)
    tmux.runner = runner
    tmux.run.side_effect = TmuxCli(runner).run
    tmux.checked.side_effect = TmuxCli(runner).checked
    tmux.has_session.return_value = False
    return tmux


class Base(Case):
    def setUp(self):
        self.dir = scratch(self)
        self.enterContext(which())
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
