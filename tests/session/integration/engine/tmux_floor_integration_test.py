"""TmuxCli.launch's min_width on a private server: an attached client narrower than the floor, at
attach or resized later, never narrows the window, and its program never sees fewer columns.
"""

import os
import sys
import tempfile
from unittest import mock

from ensemble.session.engine.tmux_cli import TmuxCli
from tests.session import live_tmux
from tests.session.case import Case

FLOOR = 40
# appends its terminal's columns whenever they may have changed
SIZES = """import os, signal, sys, time
def note(*_):
    with open(sys.argv[1], 'a') as f: f.write('%d\\n' % os.get_terminal_size(0).columns)
signal.signal(signal.SIGWINCH, note)
while True:
    note()
    time.sleep(0.05)"""


class FloorTest(Case):
    def setUp(self):
        self.inner = live_tmux.Server(self, self.home)
        self.outer = live_tmux.Server(self, self.home)
        self.enterContext(mock.patch.dict(os.environ, self.inner.env(), clear=True))
        self.sizes = os.path.join(self.home, "sizes")
        handover = self.enterContext(tempfile.TemporaryDirectory(dir=self.home))
        TmuxCli().launch(
            "w",
            [sys.executable, "-I", "-c", SIZES, self.sizes],
            cwd=self.home,
            env={},
            python=sys.executable,
            handover_dir=handover,
            min_width=FLOOR,
        )
        live_tmux.wait(lambda: self.seen(), what="the program's first size")

    def seen(self) -> list[int]:
        return [int(line) for line in live_tmux.events(self.sizes)]

    def window(self) -> str:
        return self.inner.tmux(
            "display-message", "-p", "-t", "=w:", "#{window_width}x#{window_height}"
        ).stdout.strip()

    def view(self, width: int, height: int) -> None:
        """An outer window of width x height running a real client of the inner session."""
        res = self.outer.tmux(
            "new-session",
            "-d",
            "-s",
            "view",
            "-x",
            str(width),
            "-y",
            str(height),
            "env",
            "-u",
            "TMUX",
            f"TMUX_TMPDIR={self.inner.root}",
            self.inner.program,
            "attach",
            "-t",
            "=w",
        )
        self.assertEqual(res.returncode, 0, res.stderr)

    def resize(self, width: int, height: int) -> None:
        res = self.outer.tmux("resize-window", "-t", "=view:", "-x", str(width), "-y", str(height))
        self.assertEqual(res.returncode, 0, res.stderr)

    def expect(self, size: str) -> None:
        live_tmux.wait(lambda: self.window() == size, what=f"the window at {size}")
        width = int(size.split("x")[0])
        live_tmux.wait(lambda: self.seen()[-1] == width, what=f"the program at {width} columns")

    def test_a_narrow_client_never_narrows_a_floored_window(self):
        self.view(37, 8)
        self.expect("40x8")
        self.assertGreaterEqual(min(self.seen()), FLOOR)

    def test_shrinking_an_attached_client_keeps_the_floor(self):
        self.view(120, 30)
        self.expect("120x30")
        self.resize(20, 10)
        self.expect("40x10")
        self.resize(50, 12)
        self.expect("50x12")
        self.assertGreaterEqual(min(self.seen()), FLOOR)
