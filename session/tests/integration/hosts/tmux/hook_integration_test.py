"""Installed hooks and process-exit hooks retain canonical events across file rotation."""

import json
import os
import sys
from pathlib import Path

from ensemble.session import Exited, Launch, SessionStatusLog, TurnEnd
from ensemble.session.engine.types import Activity, Blocked
from ensemble.session.modes import tui

from session.tests import live_tmux
from session.tests.tui_support import Live, Stream


class HookLifecycleTest(Live):
    def test_tui_without_a_caller_record_publishes_none(self):
        session = self.start("no-record", {"read": True})
        self.assertIsNone(session.record())
        self.assertEqual(self.server.option("no-record", "@record"), "")

    def assert_events(self, path, name, *words):
        lines = Path(path).read_text().splitlines()
        self.assertEqual(len(lines), len(words))
        for line, word in zip(lines, words, strict=True):
            clock = line.split(" ", 1)[0]
            self.assertRegex(clock, r"\A\d\d:\d\d:\d\d\Z")
            label, *fields = word.split()
            self.assertEqual(line, SessionStatusLog.line(name, label, *fields, at=clock))

    def rotate(self, suffix):
        retired = Path(self.events + suffix)
        os.replace(self.events, retired)
        Path(self.events).touch(mode=0o600)
        return retired, retired.read_bytes()

    def test_installed_hooks_and_natural_exit_write_once_to_each_rotated_file(self):
        name = "actual-hook-target"
        session = self.start(
            name,
            {"hook": "tool"},
            {"read": True},
            {"hook": "stop", "payload": {"last_message": "reply"}},
            {"read": True},
            {"hook": "ask"},
            {"read": True},
            {"exit": 0},
        )
        stream = Stream(self, session)
        self.assertEqual(stream.next(idle=False), Activity(""))
        live_tmux.wait(lambda: self.server.option(name, "@state") == "working", what="working mark")
        self.assert_events(self.events, name)

        session.send("done")
        self.assertEqual(stream.next(idle=False), TurnEnd("reply", 0))
        self.lines(f"{name} done")
        self.assert_events(self.events, name, "done")
        done_file, done_bytes = self.rotate(".done")

        session.send("blocked")
        self.assertEqual(stream.next(idle=False), Blocked(""))
        self.lines(f"{name} blocked")
        self.assert_events(self.events, name, "blocked")
        self.assertEqual(done_file.read_bytes(), done_bytes)
        blocked_file, blocked_bytes = self.rotate(".blocked")

        session.send("exit")
        self.assertEqual(stream.next(idle=False, poke=self.poke), Exited(0))
        self.lines(f"{name} dead")
        self.assert_events(self.events, name, "dead")
        self.assertEqual(self.server.option(name, "@state"), "dead")
        self.assertEqual(done_file.read_bytes(), done_bytes)
        self.assertEqual(blocked_file.read_bytes(), blocked_bytes)
        journal = Path(self.ensemble, "tui", name, "hooks.jsonl")
        self.assertEqual(
            [json.loads(line)["label"] for line in journal.read_text().splitlines()],
            ["tool", "stop", "ask"],
        )

    def test_a_fast_terminal_exit_writes_one_dead_event_and_retains_its_session(self):
        name = "fast-terminal"
        session = self.start(name, {"exit": 7})
        stream = Stream(self, session)
        self.assertEqual(stream.next(idle=False, poke=self.poke), Exited(7))
        self.lines(f"{name} dead")
        self.assertEqual(self.server.option(name, "@state"), "dead")
        self.assertEqual([row.state for row in self.session_cls.list(name)], ["dead"])
        self.assertEqual(self.server.tmux("has-session", "-t", f"={name}").returncode, 0)
        self.poke()
        self.poke()
        self.assert_events(self.events, name, "dead")
        tui.TuiStore.attach(name, host=self.session_cls.host()).close()
        self.assertEqual(self.session_cls.list(name), [])
        self.assert_events(self.events, name, "dead")

    def test_successful_detach_writes_one_exit_event_before_its_session_is_forgotten(self):
        name = "successful-detach"
        host = self.session_cls.host()
        process = host.detach(
            name,
            Launch((sys.executable, "-I", "-c", "pass"), env=self.server.env(), cwd=self.home),
            events=self.events,
        )
        self.addCleanup(process.close)

        def forgotten():
            self.poke()
            if name in host.list():
                return False
            self.assert_events(self.events, name, "exited 0")
            return True

        live_tmux.wait(forgotten, what="successful detach removed after its exit event")
        self.assertTrue(process.wait_exit(0))
        self.assertFalse(process.alive())
        self.poke()
        self.assert_events(self.events, name, "exited 0")
