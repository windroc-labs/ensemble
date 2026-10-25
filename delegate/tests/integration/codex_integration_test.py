"""Public CLI runs and answers as subprocesses, --client codex, over
session/tests/integration/clients/codex/fake_codex.py (on PATH as `codex`); HOME, its recovery
store, outside the temp dirs (as the Codex Client's tests), the workdir in one, away from any
ancestor's .codex. Main's tests/integration/core/fake_codex_integration_test.py, the Delegate's
part.
"""

import json
import os
import unittest
from unittest import mock

from ensemble.delegate.engine.runner import FIX
from ensemble.delegate.engine.types import Layout

from delegate.tests.integration.cli import (
    ASKING,
    DONE,
    NATIVE,
    UNSAVED,
    UNSAVED_ERROR,
    CodexIntegration,
)
from delegate.tests.integration.cli import codex_said as said
from delegate.tests.replies import block, mark
from session.tests.integration.clients.codex import fake_codex


class Codex(CodexIntegration):
    def test_reports_start_and_valid_outcome(self):
        res = self.drive(said(mark("start", "echoing"), "Codex says hello.", block(DONE)))
        self.assertEqual(res.returncode, 0, res.stderr)
        for line in ("Progress (start): echoing", "Codex says hello.", "delegate: status done"):
            self.assertIn(line, res.stderr)
        self.assertEqual(self.read(self.out), "Hello.\n")
        self.assertEqual(
            self.kinds(),
            ["session", "input", "client_session", "session_metadata", "progress", "result", "end"],
        )
        self.assertEqual(self.texts("client_session", "client_id"), [NATIVE])
        (call,) = self.calls()
        self.assertTrue(call["stdin_eof"])
        self.assertEqual(
            (call["cwd"], call["argv"][:2], call["resume_id"]),
            (Layout(self.work).agent, ["exec", "--json"], None),
        )
        self.assertIn("# Input\n\nHello.", call["prompt"])

    def test_writable_roots_never_hold_the_workdir_or_its_run_dir(self):
        extra, layout = os.path.join(self.home, "extra"), Layout(self.work)
        os.mkdir(extra)
        os.makedirs(os.path.join(self.home, ".ensemble"))
        with open(os.path.join(self.home, ".ensemble", "config.local.toml"), "w") as f:
            f.write(f"[delegate.roles.{self.ROLE}]\nwrite = {json.dumps([extra])}\n")
        res = self.drive(said(block(DONE)))
        self.assertEqual(res.returncode, 0, res.stderr)
        (call,) = self.calls()
        argv = call["argv"]
        added = [argv[i + 1] for i, word in enumerate(argv) if word == "--add-dir"]
        recorded = []
        for top, _dirs, names in os.walk(os.path.join(self.home, ".ensemble", "sessions")):
            for name in names:
                with open(os.path.join(top, name)) as f:
                    recorded += json.load(f).get("writable_roots", [])
        self.assertEqual((call["cwd"], added), (layout.agent, [extra]))
        self.assertTrue({layout.agent, extra} <= set(recorded), recorded)
        for root in (*added, *recorded):
            self.assertNotIn(os.path.realpath(root), {self.work, layout.run})

    def test_the_auto_mode_reaches_the_argv_before_the_configs_flags(self):
        res = self.drive(said(block(DONE)))
        self.assertEqual(res.returncode, 0, res.stderr)
        (call,) = self.calls()
        self.assertEqual(
            call["argv"][:11],
            [
                "exec",
                "--json",
                "--skip-git-repo-check",
                "-s",
                "danger-full-access",
                "-c",
                'approval_policy="on-request"',
                "-c",
                'approvals_reviewer="auto_review"',
                "-c",
                "plugins.ensemble@windroc.enabled=false",
            ],
        )
        self.assertNotIn("-s", call["argv"][11:])
        self.assertEqual(call["argv"].count("plugins.ensemble@windroc.enabled=false"), 1)
        for feature in ("apps", "tool_suggest"):
            self.assertEqual(call["argv"].count(feature), 1)
            self.assertEqual(call["argv"][call["argv"].index(feature) - 1], "--disable")
        self.assertFalse([w for w in call["argv"] if w.startswith("sandbox_workspace_write.")])

    def test_two_runs_resume_the_same_native_session_and_keep_previous_progress(self):
        first = self.drive(said(mark("start", "first turn"), block(ASKING)))
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.answer(said(mark("review", "second turn"), block(DONE)))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual([call["resume_id"] for call in self.calls()], [None, NATIVE])
        self.assertEqual(self.texts("progress"), ["first turn", "second turn"])
        self.assertEqual(self.texts("client_session", "client_id"), [NATIVE, NATIVE])

    def test_an_answer_resumes_the_same_run_and_keeps_progress(self):
        first = self.drive(said(mark("start", "before answer"), block(ASKING)))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertFalse(os.path.exists(self.out))
        second = self.answer(said(mark("review", "recovered"), block(DONE)))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.calls()[-1]["resume_id"], NATIVE)
        self.assertEqual(self.texts("client_session", "client_id"), [NATIVE, NATIVE])
        self.assertEqual(self.texts("progress"), ["before answer", "recovered"])
        self.assertEqual(len([e for e in self.records() if "outcome" in e]), 2)
        self.assertEqual(self.read(self.out), DONE["deliverable"])

    def test_missing_mapping_fails_before_spawn(self):
        first = self.drive(said(block(ASKING)))
        self.assertEqual(first.returncode, 0, first.stderr)
        store = os.path.join(self.home, ".ensemble", "sessions", "codex")
        mappings = [
            os.path.join(store, name) for name in os.listdir(store) if name.endswith(".json")
        ]
        self.assertEqual(len(mappings), 1)
        os.unlink(mappings[0])
        res = self.answer([])
        self.assertEqual((res.returncode, res.stdout), (2, ""), res.stderr)
        self.assertIn("missing Codex recovery state", res.stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_missing_native_file_fails_before_spawn(self):
        first = self.drive(said(block(ASKING)))
        self.assertEqual(first.returncode, 0, first.stderr)
        with mock.patch.dict(os.environ, {"CODEX_HOME": self.env["CODEX_HOME"]}):
            os.unlink(fake_codex.session_path(NATIVE))
        res = self.answer([])
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn("missing Codex session file", res.stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_a_bare_json_final_reply_is_no_outcome(self):
        reply = json.dumps(DONE)
        res = self.drive(said(mark("start", "working"), reply))
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn(reply, res.stderr)
        self.assertIn("delegate: the agent run returned no outcome", res.stderr)
        self.assertEqual(self.records()[-2]["error"], "the agent run returned no outcome")
        self.assertFalse(os.path.exists(self.out))

    def test_an_invalid_outcome_is_fixed_by_one_resume(self):
        res = self.drive(said(mark("start", "working"), block(UNSAVED)), fixes=said(block(DONE)))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"delegate: invalid outcome: {UNSAVED_ERROR}; resuming once", res.stderr)
        first, second = self.calls()
        self.assertEqual((first["resume_id"], second["resume_id"]), (None, NATIVE))
        self.assertTrue(second["prompt"].startswith(FIX.format(error=UNSAVED_ERROR)))
        self.assertEqual(
            self.kinds(),
            [
                "session",
                "input",
                "client_session",
                "session_metadata",
                "progress",
                "invalid",
                "session",
                "client_session",
                "result",
                "end",
            ],
        )
        self.assertEqual(self.read(self.out), DONE["deliverable"])


if __name__ == "__main__":
    unittest.main()
