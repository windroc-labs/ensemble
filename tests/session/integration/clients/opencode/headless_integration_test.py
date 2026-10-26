"""Headless opencode sessions over fake_opencode.py (on PATH as `opencode`) on the tmux Host."""

import dataclasses
import json
import os
import shlex
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from ensemble.session import Session
from ensemble.session.engine.types import (
    Access,
    Activity,
    ConfigError,
    Exited,
    Mode,
    Reply,
    Spec,
    TurnEnd,
    TurnFailed,
)
from tests.session.case import REPO, Case, configured
from tests.session.integration.clients.opencode import fake_opencode

NATIVE = fake_opencode.NATIVE


class HeadlessSessions(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("ENSEMBLE_TEST_TMP", REPO))
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.cwd, self.bin = self.root / "project", self.root / "bin"
        for path in (self.cwd, self.bin):
            path.mkdir()
        executable = self.bin / "opencode"
        executable.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(fake_opencode.__file__)} "$@"\n'
        )
        executable.chmod(0o755)
        self.log, self.scenario = self.root / "calls.jsonl", self.root / "scenario.json"
        self.scene()
        self.session_cls = configured(
            {
                "opencode": {
                    "implementation": "ensemble.session.clients.opencode:OpenCodeClient",
                    "tiers": {"1": "ollama/qwen3.6:35b-mlx"},
                    "env": {"FAKE_LOG": str(self.log), "FAKE_SCENARIO": str(self.scenario)},
                }
            }
        )
        self.enterContext(
            mock.patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ["PATH"]})
        )
        self.spec = Spec(
            workdir=str(self.root),
            access=Access(cwd=str(self.cwd), project=True),
            sid="run-1",
            tier=1,
            client="opencode",
            mode=Mode.HEADLESS,
        )

    def scene(self, steps=None, **fields):
        if steps is not None:
            fields["steps"] = steps
        self.scenario.write_text(json.dumps(fields))

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def run_session(self, prompt: str, *, resume: bool = False) -> list:
        handle: Session = self.session_cls.start(
            dataclasses.replace(self.spec, prompt=prompt, resume=resume)
        )
        self.addCleanup(handle.stop)
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            return executor.submit(lambda: list(handle.events())).result(timeout=30)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def test_a_run_reaches_the_hosts_ollama_and_binds_its_session(self):
        events = self.run_session("Hi there\n- a list")
        self.assertIn(Reply("one"), events)
        self.assertIn(TurnEnd("two"), events)
        self.assertIn(Exited(0), events)
        (call,) = self.calls()
        self.assertEqual(
            call["argv"][:5], ["run", "--format", "json", "--model", "ollama/qwen3.6:35b-mlx"]
        )
        self.assertEqual(call["prompt"], "Hi there\n- a list")
        self.assertEqual((call["cwd"], call["session"], call["auto"]), (str(self.cwd), None, False))
        provider = call["config"]["provider"]["ollama"]
        self.assertEqual(provider["options"]["baseURL"], "http://127.0.0.1:11434/v1")
        self.assertEqual(call["config"]["permission"]["bash"], "ask")
        self.assertEqual(call["env"]["OPENCODE_DISABLE_AUTOUPDATE"], "1")

    def test_rejected_headless_permission_fails_the_turn(self):
        self.scene([{"bash": "rm -rf build"}, {"text": "I could not run it."}])
        events = self.run_session("Clean up.")
        self.assertIn(Activity("bash"), events)
        self.assertIn(TurnFailed("permission_required", fake_opencode.REJECTED), events)
        self.assertFalse(any(isinstance(event, TurnEnd) for event in events))
        self.assertEqual(self.calls()[0]["refused"], ["rm -rf build"])

    def test_a_resume_continues_the_same_session(self):
        self.run_session("First.")
        self.scene(resume=[{"text": "again"}])
        events = self.run_session("Fix it.", resume=True)
        self.assertIn(TurnEnd("again"), events)
        first, second = self.calls()
        self.assertEqual((first["session"], second["session"]), (None, NATIVE))
        self.assertEqual(second["prompt"], "Fix it.")

    def test_a_2x_opencode_is_refused(self):
        self.session_cls = configured(
            {
                "opencode": {
                    "implementation": "ensemble.session.clients.opencode:OpenCodeClient",
                    "tiers": {"1": "ollama/qwen3.6:35b-mlx"},
                    "env": {
                        "FAKE_LOG": str(self.log),
                        "FAKE_SCENARIO": str(self.scenario),
                        "FAKE_OPENCODE_VERSION": "2.0.32",
                    },
                }
            }
        )
        with self.assertRaisesRegex(
            ConfigError,
            r"opencode 2\.0\.32 \(opencode\) is not 1\.x: set \[clients\.opencode\] executable",
        ):
            self.session_cls.start(dataclasses.replace(self.spec, prompt="Hi."))
        self.assertFalse(self.log.exists())

    def test_an_error_fails_the_turn(self):
        error = {"name": "UnknownError", "data": {"message": "Model not found: ollama/x."}}
        self.scene([{"error": error}, {"exit": 1}])
        events = self.run_session("Hi.")
        self.assertIn(TurnFailed("UnknownError", "Model not found: ollama/x."), events)
        self.assertNotIn(TurnEnd(""), events)
