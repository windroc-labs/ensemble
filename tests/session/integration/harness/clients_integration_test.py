"""Each built-in client's tui worker under the session harness, on a private tmux server with that
client's fake: a registered handler sees the same turn events from Claude, Codex and OpenCode.
"""

import json
import os
import shlex
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

from ensemble.session.engine.hosted_session import HostedSession
from ensemble.session.engine.types import Access, Mode, Spec
from ensemble.session.harness.view import OwnedSession
from tests.session import live_tmux
from tests.session.case import REPO, Case, harnessed
from tests.session.cli_support import WorkerCase
from tests.session.integration.clients.codex import fake_tui
from tests.session.integration.clients.opencode import fake_opencode
from tests.session.integration.harness.support import Recording
from tests.utils.sandbox import scratch

TWO_TURNS = ["TurnStarted", "TurnEnd", "TurnStarted", "TurnEnd", "Exited"]
WAIT = 30
NODE = shutil.which("node")
OPENCODE_TUI = os.path.join(
    os.path.dirname(os.path.abspath(fake_opencode.__file__)), "fake_tui.mjs"
)


class Conversation(Recording, unittest.TestCase):
    """self.server, the private tmux server."""

    server: live_tmux.Server

    def wait_for(self, kind: str, count: int = 1) -> list[dict]:
        def found():
            self.server.tmux("run-shell", "-b", "true")
            records = self.handled()
            return records if sum(r["type"] == kind for r in records) >= count else None

        return live_tmux.wait(found, WAIT, what=f"{count} x {kind} in {self.out}")

    def owned(self, session_cls: type[HostedSession], spec: Spec) -> OwnedSession:
        session = session_cls.start(spec)
        self.assertIsInstance(session, OwnedSession)
        assert isinstance(session, OwnedSession)
        self.addCleanup(session.stop)
        return session

    def send(self, session: OwnedSession, text: str) -> None:
        """Once the harness has typed the client's opening line, so this prompt does not defer it."""
        live_tmux.wait(lambda: session.opening_pending is None, WAIT, what="the opening sent")
        session.send(text)

    def converse(self, session_cls: type[HostedSession], spec: Spec) -> list[str]:
        """Prompt, TurnEnd, send, TurnEnd, close: the handler's turn event types."""
        session = self.owned(session_cls, spec)
        self.wait_for("TurnEnd")
        self.send(session, "next")
        self.wait_for("TurnEnd", 2)
        session.close()
        return [r["type"] for r in self.wait_for("Exited")]


class ClaudeTest(Conversation, WorkerCase):
    def test_two_turns_give_the_shared_sequence(self):
        self.out = os.path.join(self.root, "handled.jsonl")
        scene = self.scene("w1", ["One."], turns=[["Two."]])
        env = {**self.env(), **scene, "ENSEMBLE_EVENTS": self.events}
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))
        session_cls = harnessed(
            {"claude": {"implementation": "ensemble.session.clients.claude:ClaudeClient"}}
        )
        spec = Spec(
            workdir=self.cwd,
            access=Access(cwd=self.cwd),
            client="claude",
            mode=Mode.TUI,
            name="w1",
            prompt="hi",
            handlers=self.handlers(),
        )
        self.assertEqual(self.converse(session_cls, spec), TWO_TURNS)


class CodexTest(Conversation, Case):
    def setUp(self):
        root = Path(scratch(self, os.environ.get("ENSEMBLE_TEST_TMP", REPO)))
        home, work = root / "home", root / "work"
        cwd, codex_home = work / "project", home / ".codex"
        for path in (home, cwd, codex_home):
            path.mkdir(parents=True, exist_ok=True)
        self.server = live_tmux.Server(self, str(home), bin_dir=str(root / "bin"))
        self.server.start("mgr")
        fake_tui.install(self.server.bin)
        self.out, self.scenario = str(root / "handled.jsonl"), root / "scenario.json"
        env = self.server.env(CODEX_HOME=str(codex_home), TZ="UTC")
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))
        self.session_cls = harnessed(
            {
                "codex": {
                    "implementation": "ensemble.session.clients.codex:CodexClient",
                    "tiers": {"1": "gpt-test"},
                    "efforts": {"low": "low"},
                    "env": {
                        "FAKE_LOG": str(root / "client.jsonl"),
                        "FAKE_SCENARIO": str(self.scenario),
                        "TZ": "UTC",
                    },
                }
            }
        )
        self.spec = Spec(
            workdir=str(work),
            tier=1,
            effort="low",
            access=Access(cwd=str(cwd), project=True),
            client="codex",
            mode=Mode.TUI,
            name="w1",
            prompt="hello",
            handlers=self.handlers(),
        )

    def scene(self, turns: list) -> None:
        self.scenario.write_text(json.dumps({"tui_turns": turns}))

    def ok(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def attached(self, session: OwnedSession) -> str:
        """A pane of mgr running session's attach command, as a user's terminal would, once it
        shows the worker's screen.
        """
        argv = shlex.split(session.attach_command())
        pane = self.ok(
            self.server.tmux(
                "split-window", "-d", "-P", "-F", "#{pane_id}", "-t", "=mgr:", "--", *argv
            )
        ).stdout.strip()
        live_tmux.wait(
            lambda: self.ok(self.server.tmux("capture-pane", "-p", "-t", pane)).stdout.strip(),
            WAIT,
            what="the attach command showing w1",
        )
        return pane

    def test_two_turns_give_the_shared_sequence(self):
        self.scene([["first"], ["second"]])
        self.assertEqual(self.converse(self.session_cls, self.spec), TWO_TURNS)

    def test_server_overloaded_without_a_hook_fails_each_turn_once(self):
        failed = {"kind": "fail", "info": "server_overloaded", "message": "try again later"}
        self.scene([[failed], [failed], ["recovered"]])
        session = self.owned(self.session_cls, self.spec)
        self.wait_for("TurnFailed")
        pane = self.attached(session)
        self.ok(self.server.tmux("send-keys", "-t", pane, "-l", "continue"))
        self.ok(self.server.tmux("send-keys", "-t", pane, "Enter"))
        self.wait_for("TurnFailed", 2)
        self.send(session, "continue again")
        self.wait_for("TurnEnd")
        session.close()
        handled = self.wait_for("Exited")
        self.assertEqual(
            [r["type"] for r in handled],
            ["TurnStarted", "TurnFailed", "TurnStarted", "TurnFailed", *TWO_TURNS[2:]],
        )
        self.assertEqual(
            [r["error"] for r in handled if r["type"] == "TurnFailed"], ["server_overloaded"] * 2
        )


@unittest.skipIf(NODE is None, "node is not on PATH: the OpenCode TUI tests run plugin.js under it")
class OpenCodeTest(Conversation, Case):
    def setUp(self):
        root = Path(scratch(self, os.environ.get("ENSEMBLE_TEST_TMP", REPO)))
        home, work = root / "home", root / "work"
        cwd = work / "project"
        for path in (home, cwd):
            path.mkdir(parents=True)
        self.server = live_tmux.Server(self, str(home), bin_dir=str(root / "bin"))
        self.server.start("mgr")
        shim = Path(self.server.bin) / "opencode"
        shim.write_text(
            "#!/bin/sh\n"
            f'case "$1" in run|--version) exec {shlex.quote(sys.executable)} '
            f'{shlex.quote(fake_opencode.__file__)} "$@";; esac\n'
            f'exec {shlex.quote(str(NODE))} {shlex.quote(OPENCODE_TUI)} "$@"\n'
        )
        shim.chmod(0o755)
        self.out, scenario = str(root / "handled.jsonl"), root / "scenario.json"
        scenario.write_text(json.dumps({"turns": [[{"text": "one"}], [{"text": "two"}]]}))
        git = {
            k: os.environ[k]
            for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")
            if k in os.environ
        }
        env = self.server.env(XDG_DATA_HOME=str(root / "data"), **git)
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))
        self.session_cls = harnessed(
            {
                "opencode": {
                    "implementation": "ensemble.session.clients.opencode:OpenCodeClient",
                    "tiers": {"1": "ollama/qwen3.6:35b-mlx"},
                    "env": {"FAKE_LOG": str(root / "client.jsonl"), "FAKE_SCENARIO": str(scenario)},
                }
            }
        )
        self.spec = Spec(
            workdir=str(work),
            access=Access(cwd=str(cwd), project=True),
            tier=1,
            client="opencode",
            mode=Mode.TUI,
            name="w1",
            prompt="Hello",
            handlers=self.handlers(),
        )

    def test_two_turns_give_the_shared_sequence(self):
        self.assertEqual(self.converse(self.session_cls, self.spec), TWO_TURNS)
