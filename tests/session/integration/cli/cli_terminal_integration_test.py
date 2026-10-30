"""ensemble-session send, read and tile (FR-31; Q15) through bin/ensemble-session on a private tmux
server.
"""

import glob
import json
import os
import shlex
import sys
from unittest import mock

from ensemble.session.engine.types import Launch
from ensemble.session.hosts.tmux import TmuxHost
from tests.session import live_tmux
from tests.session.cli_support import MANAGER, CliCase

TEXT = 'text with "quotes", unicode é and spaces'
SENTINEL = "."
# Puts its tty in raw mode and appends each byte it reads to argv[1], for at most 120 s.
RAW = """import os, select, sys, time, tty
tty.setraw(0)
end = time.monotonic() + 120
with open(sys.argv[1], "ab", buffering=0) as log:
    while time.monotonic() < end:
        if select.select([0], [], [], 0.2)[0]:
            data = os.read(0, 1024)
            if not data:
                break
            log.write(data)
"""
LOGGED = """import os, select, sys, time
end = time.monotonic() + 120
with open(sys.argv[1], "ab", buffering=0) as log:
    while time.monotonic() < end:
        if select.select([0], [], [], 0.2)[0]:
            data = os.read(0, 1024)
            if not data:
                break
            log.write(data)
"""
BAD_KEY = (
    "ensemble-session: key 'Bogus': want one printable character or one of Enter, Escape, Tab, "
    "BTab, BSpace, Up, Down, Left, Right, Home, End, PageUp, PageDown, F1, F2, F3, F4, F5, F6, F7, "
    "F8, F9, F10, F11, F12, after any of C-, M-, S-\n"
)
TEXT_OR_KEYS = "ensemble-session: send: give TEXT or --key K…, not both\n"


def raw(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return b""


class Base(CliCase):
    def usage(self, result, line: str) -> None:
        """An argparse error: exit 2, nothing on stdout, `line` last on stderr."""
        self.assertEqual((result.returncode, result.stdout), (2, ""), result.stderr)
        self.assertEqual(result.stderr.splitlines()[-1], line, result.stderr)

    def failed(self, result, head: str = "ensemble-session: ") -> None:
        self.assertEqual((result.returncode, result.stdout), (1, ""), result.stderr)
        self.assertTrue(result.stderr.startswith(head), result.stderr)

    def tmux(self, *args: str) -> str:
        return self.ok(self.server.tmux(*args)).stdout.removesuffix("\n")

    def under_server_env(self, call) -> None:
        with mock.patch.dict(os.environ, self.server.env(), clear=True):
            call()


class SendReadTest(Base):
    def prompts(self, sid: str) -> list[str]:
        """The user turns the fake claude kept in its transcript of session `sid`."""
        (path,) = glob.glob(os.path.join(self.home, ".claude", "projects", "*", f"{sid}.jsonl"))
        with open(path, encoding="utf-8") as f:
            entries = [json.loads(line) for line in f]
        return [e["message"]["content"] for e in entries if e["type"] == "user"]

    def test_send_pastes_the_text_once_then_submits_it(self):
        record = self.record(self.start("w1", turns=[["Second."], ["Third."]]))
        self.wait_event("w1", "done")
        res = self.ok(self.session("send", "w1", TEXT))
        self.assertEqual((res.stdout, res.stderr), ("", ""))
        self.wait_event("w1", "done", 2)
        self.assertEqual(self.prompts(record.sid), ["hi", TEXT])
        self.ok(self.session("send", "w1", "--", "-leading-dash"))
        self.wait_event("w1", "done", 3)
        self.assertEqual(self.prompts(record.sid), ["hi", TEXT, "-leading-dash"])
        self.assertEqual(self.ok(self.session("reply", "w1")).stdout, "Third.\n")
        self.assertEqual(len(self.calls("w1")), 1)

    def test_read_prints_the_screen_or_its_last_lines(self):
        self.record(self.start("w1", steps=["one", "two", "three", "four"]))
        self.wait_event("w1", "done")

        def echoed() -> str:
            """The tty echoes the launch's /color, typed once due, wherever the fake's lines are."""
            out = self.ok(self.session("read", "w1")).stdout
            return out if "/color" in out else ""

        screen = live_tmux.wait(echoed, what="the /color echo")
        self.assertEqual(screen.count("/color"), 1)
        self.assertEqual(screen.replace("/color", "").split(), ["one", "two", "three", "four"])
        res = self.ok(self.session("read", "w1", "--lines", "3"))
        self.assertEqual(
            (res.stdout, res.stderr), ("".join(screen.splitlines(keepends=True)[-3:]), "")
        )

    def test_bad_operands_exit_2_and_an_absent_session_1(self):
        self.record(self.start("w1"))
        self.wait_event("w1", "done")
        (call,) = self.calls("w1")
        positive = "ensemble-session read: error: argument --lines: want a positive int, not {!r}"
        for value in ("0", "x", "-1"):
            with self.subTest(lines=value):
                res = self.session("read", "w1", f"--lines={value}")
                self.usage(res, positive.format(value))
        name = (
            "ensemble-session {}: error: argument name: want [A-Za-z0-9_-]+ but not none, not 'a b'"
        )
        self.usage(self.session("read", "a b"), name.format("read"))
        self.usage(self.session("send", "a b", "hi"), name.format("send"))
        for absent in ("w9", "w"):
            with self.subTest(absent=absent):
                self.failed(self.session("read", absent))
                self.failed(self.session("send", absent, "hi"))
                self.failed(self.session("send", absent, "--key", "Enter"))
        self.assertEqual(self.calls("w1"), [call])
        self.assertEqual(
            [line.split(" ")[1:] for line in live_tmux.events(self.events)], [["w1", "done"]]
        )


class KeyTest(Base):
    def setUp(self):
        super().setUp()
        self.keys = os.path.join(self.root, "keys")
        program = os.path.join(self.root, "raw.py")
        with open(program, "w", encoding="utf-8") as f:
            f.write(RAW)
        shim = os.path.join(self.root, "shim")
        os.mkdir(shim)
        with open(os.path.join(shim, "claude"), "w", encoding="utf-8") as f:
            q = shlex.quote
            f.write(f"#!/bin/sh\nexec {q(sys.executable)} -I {q(program)} {q(self.keys)}\n")
        os.chmod(os.path.join(shim, "claude"), 0o755)
        self.path = os.pathsep.join((shim, self.env()["PATH"]))

    def raw_worker(self) -> None:
        """Worker w1, its client the raw-mode key logger, once its tty is raw."""
        self.record(self.start("w1", prompt="", PATH=self.path))
        live_tmux.wait(lambda: os.path.exists(self.keys), what="w1's key log")

    def delivered(self, name: str, log: str, *argv: str):
        """`send NAME ARGV…`'s result and the bytes it put on the program's tty: those before a
        SENTINEL key sent after it.
        """
        before = len(raw(log))
        result = self.session("send", name, *argv)
        self.tmux("send-keys", "-t", self.server.pane_of(name), "--", SENTINEL)

        def found():
            data = raw(log)[before:]
            return data if data.endswith(SENTINEL.encode()) else None

        data = live_tmux.wait(found, what=f"{SENTINEL!r} on {name}'s tty")
        return result, data[: -len(SENTINEL)]

    def test_keys_reach_the_client_as_exactly_their_bytes(self):
        self.raw_worker()
        for keys, want in (
            (["Escape"], b"\x1b"),
            (["3", "Up", "BTab", "C-c"], b"3\x1b[A\x1b[Z\x03"),
            (["C- ", "C-/", "C-[", "C-]", "C-\\", "C-?"], b"\x00\x1f\x1b\x1d\x1c\x7f"),
        ):
            with self.subTest(keys=keys):
                res, data = self.delivered("w1", self.keys, "--key", *keys)
                self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))
                self.assertEqual(data, want)

    def test_a_bad_key_list_or_text_with_keys_or_neither_exits_2_and_sends_nothing(self):
        self.raw_worker()
        for argv, error in (
            (["--key", "Escape", "Bogus"], BAD_KEY),
            (["text", "--key", "Escape"], TEXT_OR_KEYS),
            ([], TEXT_OR_KEYS),
        ):
            with self.subTest(argv=argv):
                res, data = self.delivered("w1", self.keys, *argv)
                self.assertEqual((res.returncode, res.stdout, res.stderr), (2, "", error))
                self.assertEqual(data, b"")

    def test_a_session_with_no_terminal_takes_neither_keys_nor_text(self):
        """A headless worker h1, its stdin logged, gets neither; a SENTINEL it is then written
        arrives alone.
        """
        log = os.path.join(self.root, "h1.keys")
        argv = (sys.executable, "-I", "-c", LOGGED, log)
        with mock.patch.dict(os.environ, self.server.env(), clear=True):
            worker = TmuxHost("tmux").worker("h1", Launch(argv, cwd=self.cwd), terminal=False)
        self.addCleanup(self.under_server_env, worker.close)
        live_tmux.wait(lambda: os.path.exists(log), what="h1's key log")
        for argv in (["--key", "Escape"], ["text"]):
            with self.subTest(argv=argv):
                before = len(raw(log))
                self.failed(
                    self.session("send", "h1", *argv), "ensemble-session: no tui session h1"
                )
                worker.write(SENTINEL.encode())
                data = live_tmux.wait(
                    lambda before=before: raw(log)[before:] or None, what="h1's sentinel"
                )
                self.assertEqual(data, SENTINEL.encode())


class TileTest(Base):
    def layout(self, target: str) -> str:
        return self.tmux("display-message", "-p", "-t", target, "#{window_layout}")

    def test_tile_lays_out_the_window_it_names_and_no_other(self):
        self.record(self.start("w1"))
        self.record(self.start("w2"))
        window = self.tmux("display-message", "-p", "-t", f"={MANAGER}:", "#{window_id}")
        panes = self.server.panes(window)
        self.assertIn(self.server.pane_of("w1"), panes)
        self.assertIn(self.server.pane_of("w2"), panes)
        grid = self.layout(window)
        other = self.tmux(
            "new-window", "-d", "-P", "-F", "#{window_id}", "-t", f"={MANAGER}:", *live_tmux.IDLE
        )
        self.tmux("split-window", "-d", "-t", other, *live_tmux.IDLE)
        self.tmux("new-session", "-d", "-s", "x1", *live_tmux.IDLE)
        self.tmux("split-window", "-d", "-h", "-t", "=x1:", *live_tmux.IDLE)
        self.tmux("resize-pane", "-t", self.server.pane_of("w1"), "-D", "5")
        for target, way in ((other, "-D"), ("=x1:", "-R")):
            pane = self.tmux("display-message", "-p", "-t", target, "#{pane_id}")
            self.tmux("resize-pane", "-t", pane, way, "3")
        dragged = self.layout(window)
        self.assertNotEqual(dragged, grid)
        untouched = {target: self.layout(target) for target in (other, "=x1:")}
        res = self.ok(self.session("tile", window))
        self.assertEqual((res.stdout, res.stderr), ("", ""))
        self.assertEqual(self.layout(window), grid)
        self.assertEqual({target: self.layout(target) for target in untouched}, untouched)

    def test_a_bad_window_id_exits_1_with_the_tmux_error(self):
        res = self.session("tile", "3")
        self.assertEqual(
            (res.returncode, res.stdout, res.stderr),
            (1, "", "ensemble-session: tile 3: invalid window id '3': want @[0-9]+\n"),
        )

    def test_a_missing_window_exits_1_with_the_tmux_error(self):
        res = self.session("tile", "@999")
        self.failed(res, "ensemble-session: tile @999: can't find window: @999")
        self.assertIn("@999", res.stderr)
