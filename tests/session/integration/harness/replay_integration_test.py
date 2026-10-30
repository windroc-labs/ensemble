"""A guard's replay through real tmux panes running the attach command: colored history in the
pane's scrollback, the cursor at the prompt, typing reaching the worker.
"""

import os
import re
import shlex
import sys
import time
from unittest import mock

from ensemble.session.engine.types import Launch
from ensemble.session.hosts.tmux import TmuxHost
from tests.session import guard_fixture, live_tmux
from tests.session.case import SID, Case

PROMPT = "> "


class ReplayTest(Case):
    NAME = "replay-worker"

    def setUp(self):
        self.server = live_tmux.Server(self, self.home)
        self.server.start("manager")
        self.server.tmux("set-option", "-g", "history-limit", "50000")
        self.enterContext(mock.patch.dict(os.environ, self.server.env(), clear=True))
        self.host = TmuxHost("tmux")

    def worker(self, count, long=0):
        launch = Launch(
            (
                sys.executable,
                "-I",
                guard_fixture.__file__,
                "history",
                self.home,
                "--count",
                str(count),
                "--long",
                str(long),
            ),
            self.server.env(),
            cwd=self.home,
        )
        worker = self.host.worker(
            self.NAME, launch, terminal=True, metadata={"sid": SID, "client": "fake"}
        )
        self.addCleanup(worker.close)
        live_tmux.wait(lambda: worker.screen(lines=1).endswith(">"), what="the worker's prompt")
        return worker

    def attach(self, pane, columns=80, rows=20):
        """A detached session of that size whose pane runs the attach command; once its prompt shows."""
        argv = self.host.attach_target(self.NAME).argv
        started = self.server.tmux(
            "new-session", "-d", "-s", pane, "-x", str(columns), "-y", str(rows), shlex.join(argv)
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        live_tmux.wait(
            lambda: self.visible(pane)[-1:] == [PROMPT.rstrip()], what=f"{pane}'s prompt"
        )

    def capture(self, pane, *more):
        result = self.server.tmux("capture-pane", "-p", "-t", f"={pane}:", *more)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def visible(self, pane):
        lines = self.capture(pane).rstrip("\n").split("\n")
        while lines and not lines[-1].strip():
            lines.pop()
        return [line.rstrip() for line in lines]

    def scrollback(self, pane):
        return self.capture(pane, "-e", "-S", "-")

    def numbered(self, pane, tag="line"):
        return [int(n) for n in re.findall(rf"{tag}-(\d{{4}})", self.scrollback(pane))]

    def test_hidden_colored_output_is_in_the_new_panes_scrollback(self):
        self.worker(60)
        self.attach("pane-a")
        text = self.scrollback("pane-a")
        for number in range(60):
            self.assertIn(f"\x1b[3{1 + number % 6}mline-{number:04}", text)

    def test_3000_hidden_lines_leave_the_last_2000_in_the_scrollback(self):
        self.worker(3000)
        self.attach("pane-a", rows=20)
        shown = self.numbered("pane-a")
        self.assertEqual((min(shown), max(shown), len(shown)), (981, 2999, 2019))

    def test_long_lines_rewrap_whole_in_a_narrower_pane_without_spinner_repeats(self):
        self.worker(40, long=3)
        self.attach("pane-a", columns=50)
        joined = self.capture("pane-a", "-J", "-S", "-").split("\n")
        for n in range(3):
            self.assertIn(f"long-{n}:" + chr(ord("a") + n) * (200 + n), joined)
        self.assertEqual([line for line in joined if "spin" in line], ["spinner-9"])

    def test_output_while_shown_and_while_hidden_is_in_the_next_pane(self):
        worker = self.worker(0)
        self.attach("pane-a")
        worker.write(b"emit 500 shown\n")
        live_tmux.wait(lambda: "shown-0499" in self.scrollback("pane-a"), what="500 shown lines")
        self.server.tmux("kill-session", "-t", "=pane-a")
        worker.write(b"emit 10 hidden\n")
        live_tmux.wait(lambda: "hidden-0009" in worker.screen(lines=3), what="10 hidden lines")
        self.attach("pane-b")
        self.assertEqual(self.numbered("pane-b", "shown"), list(range(500)))
        self.assertEqual(self.numbered("pane-b", "hidden"), list(range(10)))

    def test_cursor_at_the_prompt_and_typing_reaches_the_worker(self):
        self.worker(5)
        self.attach("pane-a", columns=60, rows=15)
        cursor = self.server.tmux(
            "display-message", "-p", "-t", "=pane-a:", "#{cursor_x},#{cursor_y}"
        )
        self.assertEqual(cursor.stdout.strip(), f"{len(PROMPT)},{len(self.visible('pane-a')) - 1}")
        self.server.tmux("send-keys", "-t", "=pane-a:", "typed words", "Enter")
        live_tmux.wait(lambda: "\x1b[32mtyped words" in self.scrollback("pane-a"), what="the echo")

    def test_a_second_pane_also_gets_history(self):
        self.worker(60)
        self.attach("pane-a")
        self.attach("pane-b", columns=70, rows=18)
        for pane in ("pane-a", "pane-b"):
            self.assertEqual(self.numbered(pane), list(range(60)))
            self.assertIn("\x1b[31mline-0000", self.scrollback(pane))

    def test_2000_row_replay_shows_within_one_second(self):
        self.worker(2000)
        started = time.monotonic()
        self.attach("pane-a")
        elapsed = time.monotonic() - started
        print(f"\n2000-row replay shown in {elapsed:.3f} s", file=sys.stderr)
        self.assertLess(elapsed, 1.0)
        self.assertEqual(max(self.numbered("pane-a")), 1999)
