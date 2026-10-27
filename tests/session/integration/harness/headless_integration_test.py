"""Each built-in client's headless run under the session harness on the tmux Host, with that client's
fake: a registered handler and the view's events() see the same turn events.
"""

import json
import os
import shlex
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from ensemble.session.engine.hosted_session import HostedSession
from ensemble.session.engine.types import (
    Access,
    Exited,
    HookCallback,
    Mode,
    NotShowable,
    SessionError,
    SessionStatus,
    Spec,
    TurnEnd,
    TurnFailed,
    TurnStarted,
)
from ensemble.session.harness.view import OwnedSession
from ensemble.session.hosts.base import Host
from tests.session import live_tmux
from tests.session.case import HOSTS, REPO, Case
from tests.session.cli_support import logged
from tests.session.integration.clients.claude import fake_claude
from tests.session.integration.clients.codex import fake_codex
from tests.session.integration.clients.opencode import fake_opencode

HANDLER = "tests.session.integration.harness.recording_handler"
TURNS = ("TurnStarted", "TurnFailed", "TurnEnd", "Exited")
WAIT = 30
CLAUDE = {
    "implementation": "ensemble.session.clients.claude:ClaudeClient",
    "tiers": {"4": "haiku"},
    "efforts": {"low": "low"},
}


def running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def harnessed(clients: dict) -> type[HostedSession]:
    """A default-configured session class on tmux, so a session harness owns its headless starts."""
    return HostedSession.configured(
        clients=clients, hosts=Host.parse_hosts({"tmux": HOSTS["tmux"], "default": "tmux"})
    )


def shim(bin_dir: Path, name: str, script: str) -> None:
    path = bin_dir / name
    path.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(script)} "$@"\n')
    path.chmod(0o755)


class Headless(Case):
    """self.root, a temp dir; the handler's records in self.out."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("ENSEMBLE_TEST_TMP", REPO))
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.bin, self.work = self.root / "bin", self.root / "work"
        self.cwd = self.work / "project"
        for path in (self.bin, self.cwd):
            path.mkdir(parents=True)
        self.out = str(self.root / "handled.jsonl")

    def handlers(self) -> tuple[HookCallback, ...]:
        return (HookCallback(HANDLER, {"path": self.out}, (REPO,)),)

    def handled(self) -> list[dict]:
        return [r for r in logged(self.out) if r["type"] in TURNS]

    def wait_for(self, kind: str) -> list[dict]:
        def found():
            records = logged(self.out)
            return records if any(r["type"] == kind for r in records) else None

        live_tmux.wait(found, WAIT, what=f"{kind} in {self.out}")
        return self.handled()

    def owned(self, spec: Spec) -> OwnedSession:
        view = self.session_cls.start(spec)
        self.assertIsInstance(view, OwnedSession)
        assert isinstance(view, OwnedSession)
        self.addCleanup(view.stop)
        return view

    def drain(self, view: OwnedSession) -> list:
        """view.events() to its end, its turn events only; fails, stopping it, past WAIT."""
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            found = executor.submit(lambda: list(view.events())).result(timeout=WAIT)
        except TimeoutError:
            view.stop()
            self.fail(f"events() did not end within {WAIT}s")
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return [e for e in found if type(e).__name__ in TURNS]

    def succeeds(self, spec: Spec, reply: str) -> None:
        view = self.owned(spec)
        self.assertEqual(self.drain(view), [TurnStarted(), TurnEnd(reply), Exited(0)])
        handled = self.wait_for("Exited")
        self.assertEqual([r["type"] for r in handled], ["TurnStarted", "TurnEnd", "Exited"])
        self.assertEqual((handled[1]["reply"], handled[2]["returncode"]), (reply, 0))


class ClaudeHeadless(Headless):
    def setUp(self):
        super().setUp()
        self.scenario, self.log = self.root / "scenario.json", self.root / "claude.jsonl"
        env = {
            "PATH": fake_claude.install(str(self.bin)),
            "CLAUDE_CONFIG_DIR": str(self.root / "config"),
            fake_claude.ENV: str(self.scenario),
        }
        self.enterContext(mock.patch.dict(os.environ, env))
        self.session_cls = harnessed({"claude": CLAUDE})

    def scene(self, **fields) -> None:
        self.scenario.write_text(json.dumps({"log": str(self.log), **fields}))

    def spec(self) -> Spec:
        return Spec(
            workdir=str(self.work),
            access=Access(cwd=str(self.cwd)),
            client="claude",
            mode=Mode.HEADLESS,
            prompt="hello",
            handlers=self.handlers(),
        )

    def pid(self) -> int:
        """The fake's, once it has started."""
        return live_tmux.wait(
            lambda: [r["pid"] for r in logged(str(self.log))], WAIT, what="the fake started"
        )[0]


class ClaudeSuccessTest(ClaudeHeadless):
    def test_a_run_ends_its_turn_then_exits_0(self):
        self.scene(steps=["Hello."])
        self.succeeds(self.spec(), "Hello.")


class ClaudeFailureTest(ClaudeHeadless):
    def test_a_nonzero_exit_fails_the_turn_then_exits_with_its_code(self):
        self.scene(steps=["partial"], exit=3)
        view = self.owned(self.spec())
        self.assertEqual(
            self.drain(view),
            [TurnStarted(), TurnFailed("error_during_execution"), Exited(3)],
        )
        handled = self.wait_for("Exited")
        self.assertEqual([r["type"] for r in handled], ["TurnStarted", "TurnFailed", "Exited"])
        self.assertEqual(handled[-1]["returncode"], 3)


class ClaudeStopTest(ClaudeHeadless):
    def test_stopping_a_hung_run_interrupts_its_turn_and_exits_once(self):
        self.scene(steps=["working"], hang=True)
        view = self.owned(self.spec())
        pid = self.pid()
        self.wait_for("Reply")
        view.stop()
        handled = self.wait_for("Exited")
        self.assertEqual([r["type"] for r in handled], ["TurnStarted", "TurnFailed", "Exited"])
        self.assertEqual(handled[1]["error"], "interrupted")
        self.assertNotEqual(handled[2]["returncode"], 0)
        live_tmux.wait(lambda: not view.alive(), WAIT, what="the view dead")
        live_tmux.wait(lambda: not running(pid), WAIT, what=f"the fake {pid} gone")
        self.assertEqual([r["type"] for r in self.handled()].count("Exited"), 1)


class ClaudeViewTest(ClaudeHeadless):
    def test_a_running_view_has_no_terminal_and_dies_with_its_run(self):
        gate = self.root / "gate"
        self.scene(steps=["Hello."], gate=str(gate))
        view = self.owned(self.spec())
        self.pid()
        self.assertEqual((view.mode, view.name), (Mode.HEADLESS, None))
        self.assertTrue(view.alive())
        self.assertEqual(view.info().sid, view.sid)
        self.assertIsInstance(view.session_status(), SessionStatus)
        with self.assertRaises(SessionError):
            view.send("more")
        with self.assertRaises(NotShowable):
            view.screen()
        with self.assertRaises(NotShowable):
            view.show()
        gate.touch()
        self.assertEqual(self.wait_for("Exited")[-1]["returncode"], 0)
        live_tmux.wait(lambda: not view.alive(), WAIT, what="the view dead")


class CodexSuccessTest(Headless):
    def test_a_run_ends_its_turn_then_exits_0(self):
        home = self.root / "home"
        codex_home = home / ".codex"
        codex_home.mkdir(parents=True)
        shim(self.bin, "codex", fake_codex.__file__)
        scenario = self.root / "scenario.json"
        scenario.write_text("{}")
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(codex_home),
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
        }
        self.enterContext(mock.patch.dict(os.environ, env))
        self.session_cls = harnessed(
            {
                "codex": {
                    "implementation": "ensemble.session.clients.codex:CodexClient",
                    "tiers": {"1": "gpt-test"},
                    "efforts": {"low": "low"},
                    "env": {
                        "FAKE_LOG": str(self.root / "exec.jsonl"),
                        "FAKE_SCENARIO": str(scenario),
                    },
                }
            }
        )
        spec = Spec(
            flags=(
                "-s",
                "danger-full-access",
                "-c",
                'approval_policy="on-request"',
                "-c",
                'approvals_reviewer="auto_review"',
            ),
            workdir=str(self.work),
            tier=1,
            effort="low",
            access=Access(cwd=str(self.cwd), project=True),
            client="codex",
            mode=Mode.HEADLESS,
            prompt="hello",
            handlers=self.handlers(),
        )
        self.succeeds(spec, "three")


class OpenCodeHeadless(Headless):
    def setUp(self):
        super().setUp()
        shim(self.bin, "opencode", fake_opencode.__file__)
        self.scenario = self.root / "scenario.json"
        self.scenario.write_text("{}")
        path = {"PATH": str(self.bin) + os.pathsep + os.environ["PATH"]}
        self.enterContext(mock.patch.dict(os.environ, path))
        self.session_cls = harnessed(
            {
                "opencode": {
                    "implementation": "ensemble.session.clients.opencode:OpenCodeClient",
                    "tiers": {"1": "ollama/qwen3.6:35b-mlx"},
                    "env": {
                        "FAKE_LOG": str(self.root / "calls.jsonl"),
                        "FAKE_SCENARIO": str(self.scenario),
                    },
                }
            }
        )
        self.spec = Spec(
            workdir=str(self.work),
            access=Access(cwd=str(self.cwd), project=True),
            tier=1,
            client="opencode",
            mode=Mode.HEADLESS,
            prompt="Hello",
            handlers=self.handlers(),
        )


class OpenCodeSuccessTest(OpenCodeHeadless):
    def test_a_run_ends_its_turn_then_exits_0(self):
        self.succeeds(self.spec, "two")


class OpenCodeStopTest(OpenCodeHeadless):
    def test_stopping_a_hung_run_interrupts_its_turn(self):
        self.scenario.write_text(json.dumps({"steps": [{"text": "working"}, {"sleep": 60}]}))
        view = self.owned(self.spec)
        self.wait_for("TurnStarted")
        view.stop()
        handled = self.wait_for("Exited")
        self.assertEqual([r["type"] for r in handled], ["TurnStarted", "TurnFailed", "Exited"])
        self.assertEqual(handled[1]["error"], "interrupted")


class OpenCodePermissionTest(OpenCodeHeadless):
    def test_an_uninteractive_permission_request_fails_the_turn(self):
        self.scenario.write_text(
            json.dumps({"steps": [{"bash": "rm -rf build"}, {"text": "I could not run it."}]})
        )
        view = self.owned(self.spec)
        found = self.drain(view)
        self.assertEqual(
            found[:-1], [TurnStarted(), TurnFailed("permission_required", fake_opencode.REJECTED)]
        )
        self.assertIsInstance(found[-1], Exited)
        handled = self.wait_for("Exited")
        self.assertEqual([r["type"] for r in handled], ["TurnStarted", "TurnFailed", "Exited"])
        self.assertEqual(
            (handled[1]["error"], handled[1]["detail"]),
            ("permission_required", fake_opencode.REJECTED),
        )
