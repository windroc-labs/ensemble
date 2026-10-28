"""Public CLI runs, --client opencode, over
tests/session/integration/clients/opencode/fake_opencode.py (on PATH as `opencode`) on the default
tmux Host, headless: the Delegate's part of the opencode client.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from ensemble.delegate.engine.runner import FIX
from ensemble.delegate.engine.types import Layout
from tests.delegate.case import PY
from tests.delegate.integration.cli import (
    DONE,
    SID,
    UNSAVED,
    UNSAVED_ERROR,
    WAIT,
    OpenCodeIntegration,
)
from tests.delegate.replies import block, mark
from tests.session import live_tmux
from tests.session.integration.clients.opencode import fake_opencode

NATIVE = fake_opencode.NATIVE


def said(*texts) -> list[dict]:
    return [{"text": text} for text in texts]


class OpenCode(OpenCodeIntegration):
    def test_reports_start_and_valid_outcome(self):
        res = self.drive(said(mark("start", "echoing"), "OpenCode says hello.", block(DONE)))
        self.assertEqual(res.returncode, 0, res.stderr)
        for line in ("Progress (start): echoing", "OpenCode says hello.", "delegate: status done"):
            self.assertIn(line, res.stderr)
        self.assertEqual(self.read(self.out), "Hello.\n")
        self.assertEqual(self.texts("client_session", "client_id"), [NATIVE])
        (call,) = self.calls()
        self.assertEqual(call["cwd"], self.proj)
        self.assertEqual(call["argv"][:3], ["run", "--format", "json"])
        self.assertIsNone(call["session"])
        self.assertIn("# Input\n\nHello.", call["prompt"])
        provider = call["config"]["provider"]["ollama"]
        self.assertEqual(provider["options"]["baseURL"], "http://127.0.0.1:11434/v1")
        self.assertFalse(call["auto"])

    def test_an_invalid_outcome_is_fixed_by_one_resume(self):
        res = self.drive(said(mark("start", "working"), block(UNSAVED)), fixes=said(block(DONE)))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"delegate: invalid outcome: {UNSAVED_ERROR}; resuming once", res.stderr)
        first, second = self.calls()
        self.assertEqual((first["session"], second["session"]), (None, NATIVE))
        self.assertTrue(second["prompt"].startswith(FIX.format(error=UNSAVED_ERROR)))
        self.assertEqual(self.texts("client_session", "client_id"), [NATIVE, NATIVE])
        self.assertEqual(self.read(self.out), DONE["deliverable"])

    def test_a_run_that_starts_no_session_fails(self):
        res = self.drive(said(block(DONE)), unbound=True)
        self.assertNotEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("client_session", self.kinds())
        self.assertIn("no_session", json.dumps(self.records()))

    def test_an_untrusted_cwd_is_refused_before_opencode_starts(self):
        res = self.drive(said(block(DONE)), trusted=False)
        self.assertNotEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"cwd {Layout(self.work).agent} is untrusted", res.stderr)
        self.assertEqual(self.calls(), [])


@unittest.skipIf(shutil.which("node") is None, "node is required for the fake OpenCode TUI")
class OpenCodeTui(OpenCodeIntegration):
    def setUp(self):
        super().setUp()
        self.server = live_tmux.Server(self, self.home)
        self.server.start("mgr")
        self.env.update(self.server.env(PATH=self.env["PATH"] + os.pathsep + self.server.path))
        fake = Path(fake_opencode.__file__).with_name("fake_tui.mjs")
        shim = Path(self.root, "bin", "opencode")
        shim.write_text(
            "#!/bin/sh\n"
            f'case "$1" in run|--version) exec {shlex.quote(sys.executable)} '
            f'{shlex.quote(fake_opencode.__file__)} "$@";; esac\n'
            f'exec {shlex.quote(str(shutil.which("node")))} {shlex.quote(str(fake))} "$@"\n'
        )

    def scene(self, steps, **scenario):
        Path(self.scenario).write_text(json.dumps({"turns": [steps], **scenario}))

    def printed(self) -> str:
        """What the run's handler printed: an owned tui run's handler runs in its session harness,
        whose stderr is its harness.log.
        """
        logs = Path(self.home, ".ensemble", "harness").glob("*/harness.log")
        return "".join(log.read_text(encoding="utf-8", errors="replace") for log in logs)

    def test_tui_records_intermediate_progress_once_and_excludes_child_replies(self):
        final = mark("step", "finished") + "\n" + block(DONE)
        res = self.drive(
            [
                {"text": mark("start", "working"), "repeat": 2},
                {"text": mark("child", "excluded"), "child": True},
                {"busy": True},
                {"reinit": True},
                {"text": "intermediate prose"},
                {"text": final, "repeat": 2, "reads": 8},
            ],
            "--mode",
            "tui",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(
            [(r["name"], r["text"]) for r in self.records() if r["kind"] == "progress"],
            [("start", "working"), ("step", "finished")],
        )
        printed = self.printed()
        self.assertEqual(printed.count("intermediate prose"), 1)
        self.assertEqual(printed.count("Progress (step): finished"), 1)
        self.assertNotIn("excluded", printed + res.stderr)
        self.assertNotIn("missing progress mark", json.dumps(self.records()))
        self.assertEqual(self.read(self.out), DONE["deliverable"])

    def test_two_sends_mid_turn_join_the_board_run_and_end_done(self):
        """A board run (detached, its events file) whose turn takes two manager sends: one result,
        outcome done (WDRC-360: turn_failed superseded, outcome error and the drive's exit 1 at the
        first send while the worker went on).
        """
        events = os.path.join(self.root, "events")
        res = self.drive(
            [{"text": mark("start", "working")}, {"join": 2}, {"text": block(DONE)}],
            *("--mode", "tui", "--detach", "--events", events),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        log = os.path.join(self.work, "run", "run.jsonl")
        live_tmux.wait(
            lambda: os.path.exists(log) and "progress" in self.kinds(), WAIT, "the start mark"
        )
        tui = f"{self.ROLE}-{SID[:8]}"
        for text in ("Also cover the registry's CLI.", "And its docs."):
            sent = subprocess.run(
                [*PY, "-m", "ensemble.session.cli", "send", tui, text],
                cwd=self.proj,
                env=self.env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=WAIT,
            )
            self.assertEqual((sent.returncode, sent.stdout), (0, ""), sent.stderr)
        drive = f"{tui}-drive "

        def drive_lines():
            rows = [row.split(" ", 1)[1] for row in live_tmux.events(events)]
            return [row.removeprefix(drive) for row in rows if row.startswith(drive)]

        live_tmux.wait(lambda: any(r.startswith("exited ") for r in drive_lines()), WAIT, "exit")
        self.assertEqual(drive_lines(), ["outcome done", "exited 0"])
        kinds = self.kinds()
        self.assertNotIn("turn_failed", kinds)
        self.assertEqual(kinds.count("result"), 1)
        self.assertEqual(self.last("result")["outcome"]["status"], "done")
        self.assertEqual(self.read(self.out), DONE["deliverable"])


if __name__ == "__main__":
    unittest.main()
