"""Container launch settings and default wheel handling through a nested tmux attachment."""

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

from ensemble.session.engine.tmux_cli import TmuxCli
from tests.session import live_tmux
from tests.session.case import Case

WHEEL_UP = "\x1b[<64;1;1M"
WHEEL_DOWN = "\x1b[<65;1;1M"
APP = """import os, sys, tty
tty.setraw(0)
if sys.argv[2] == 'mouse':
    os.write(1, b'\\x1b[?1000h\\x1b[?1006h')
os.write(1, ''.join('history %d\\r\\n' % i for i in range(2200)).encode())
while True:
    data = os.read(0, 1024)
    with open(sys.argv[1], 'ab') as f: f.write(data)
"""


class ScrollbackTest(Case):
    def setUp(self):
        self.inner = live_tmux.Server(self, self.home)
        self.outer = live_tmux.Server(self, self.home)
        self.enterContext(mock.patch.dict(os.environ, self.inner.env(), clear=True))
        self.received = Path(self.home, "received")

    def launch(self, *, mouse: bool):
        handover = self.enterContext(tempfile.TemporaryDirectory(dir=self.home))
        TmuxCli().launch(
            "w",
            [sys.executable, "-I", "-c", APP, str(self.received), "mouse" if mouse else "plain"],
            cwd=self.home,
            env={},
            python=sys.executable,
            handover_dir=handover,
        )
        live_tmux.wait(
            lambda: int(self.value("history_size")) > 2000, what="pane history over 2000"
        )
        self.assertEqual(self.value("history_limit"), "50000")
        self.assertEqual(self.value("mouse_any_flag"), "1" if mouse else "0")
        result = self.outer.tmux(
            "new-session",
            "-d",
            "-s",
            "view",
            "env",
            "-u",
            "TMUX",
            f"TMUX_TMPDIR={self.inner.root}",
            self.inner.program,
            "attach",
            "-t",
            "=w",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        live_tmux.wait(
            lambda: self.value("mouse_any_flag", server=self.outer, session="view") == "1",
            what="nested attachment requesting mouse events",
        )

    def value(self, name: str, *, server=None, session="w") -> str:
        result = (server or self.inner).tmux(
            "display-message", "-p", "-t", f"={session}:", "#{" + name + "}"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def send(self, text: str):
        result = self.outer.tmux("send-keys", "-t", "=view:", "-l", text)
        self.assertEqual(result.returncode, 0, result.stderr)

    def wheel(self, *, up: bool):
        # SGR coordinates are 1-based; pane_top skips launch's top border.
        x = int(self.value("pane_left")) + 1
        y = int(self.value("pane_top")) + 1
        self.send(f"\x1b[<{64 if up else 65};{x};{y}M")

    def read(self) -> bytes:
        return self.received.read_bytes() if self.received.exists() else b""

    def test_wheel_enters_history_and_returning_to_bottom_restores_input(self):
        self.launch(mouse=False)
        self.wheel(up=True)
        live_tmux.wait(lambda: self.value("pane_in_mode") == "1", what="wheel entering copy mode")
        self.wheel(up=False)
        live_tmux.wait(lambda: self.value("pane_in_mode") == "0", what="wheel leaving copy mode")
        self.send("ready")
        live_tmux.wait(lambda: b"ready" in self.read(), what="input reaching the application")

    def test_mouse_capturing_application_keeps_both_wheel_events(self):
        self.launch(mouse=True)
        self.wheel(up=True)
        self.wheel(up=False)
        live_tmux.wait(
            lambda: (WHEEL_UP + WHEEL_DOWN).encode() in self.read(),
            what="both wheel events reaching the application",
        )
        self.assertEqual(self.value("pane_in_mode"), "0")
