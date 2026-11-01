"""CLI runs and API resumes over tests/session/integration/clients/claude/fake_claude.py (on
PATH as `claude`), HOME and CLAUDE_CONFIG_DIR temp: a run's exit code, stderr, out and run.jsonl;
its fix resume, a resume, a draft kept through a kill and resume, an interrupt, a usage limit,
config errors. Main's tests/integration/core/drive_integration_test.py and
resume_integration_test.py, the Delegate's part.
"""

import json
import os
import signal
import unittest
from datetime import UTC, datetime
from unittest import mock

from ensemble.delegate.composer.prompt import RESUME
from ensemble.delegate.engine.runner import FIX
from ensemble.delegate.engine.types import Layout
from ensemble.delegate.event.reply import OUTCOME
from ensemble.session import Access, Spec
from tests.delegate.case import packaged
from tests.delegate.integration.cli import (
    ASKING,
    DONE,
    RESETS,
    RUN,
    SID,
    UNSAVED,
    UNSAVED_ERROR,
    WAIT,
    Integration,
)
from tests.delegate.replies import bare, block, mark, option, prompt
from tests.session import live_tmux
from tests.utils.sandbox import pid_exists as running

ANSWER = "Say hello."
DRAFT = "Draft.\n"


class NewRun(Integration):
    def test_done(self):
        res = self.drive([mark("start", "echoing"), "Echoing.", block(DONE)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "")
        for line in (
            f"delegate: session {SID}",
            "Progress (start): echoing",
            "Echoing.",
            "delegate: status done",
        ):
            self.assertIn(line, res.stderr)
        self.assertEqual(self.read(self.out), "Hello.\n")
        self.assertEqual(
            self.kinds(), ["session", "input", "client_session", "progress", "result", "end"]
        )
        session = self.records()[0]
        self.assertEqual(
            (session["sid"], session["cwd"], session["project"], session["transcript"]),
            (SID, self.agent, False, self.transcript),
        )
        self.assertEqual(
            [*map(bare, self.records()[1:])],
            [
                {"kind": "input", "text": "Hello."},
                {"kind": "client_session", "sid": SID, "client_id": SID},
                {"kind": "progress", "name": "start", "text": "echoing"},
                {
                    "kind": "result",
                    "outcome": {
                        "status": "done",
                        "title": "hello",
                        "summary": "Hello.",
                        "questions": [],
                        "url": self.out,
                        "files": [],
                    },
                },
                {"kind": "end", "sid": SID, "rc": 0},
            ],
        )
        self.assertEqual(set(os.listdir(self.work)), {"agent", "run", "out.md"})
        self.assertTrue(os.path.isfile(self.transcript))
        (call,) = self.calls()
        self.assertEqual(
            (call["cwd"], option(call, "--session-id"), option(call, "--output-format")),
            (self.agent, SID, "stream-json"),
        )
        self.assertIsNone(option(call, "--resume"))

    def test_the_default_policy_and_auto_mode_reach_the_argv_once_each(self):
        res = self.drive([block(DONE)])
        self.assertEqual(res.returncode, 0, res.stderr)
        (call,) = self.calls()
        argv = call["argv"]
        mode = argv.index("--permission-mode")
        self.assertEqual(argv[mode : mode + 2], ["--permission-mode", "auto"])
        policy = argv.index("--strict-mcp-config")
        rule = argv[policy + 1]
        self.assertEqual(argv[policy + 2], "--")
        self.assertTrue(rule.startswith("--append-system-prompt=") and f"`{OUTCOME}`" in rule, rule)
        self.assertEqual(
            argv[argv.index("--settings") + 1], '{"enabledPlugins": {"ensemble@windroc": false}}'
        )
        for flag in ("--permission-mode", "--strict-mcp-config", "--settings", rule):
            self.assertEqual(argv.count(flag), 1, flag)

    def test_add_dirs_never_hold_the_workdir_or_its_run_dir(self):
        layout = Layout(self.work)
        for extra, cwd, want in (
            ((), self.agent, []),
            (("--cwd", self.proj), self.proj, [self.agent]),
        ):
            with self.subTest(cwd=cwd):
                res = self.drive([mark("start", "echoing"), block(DONE)], *extra)
                self.assertEqual(res.returncode, 0, res.stderr)
                argv = self.calls()[-1]["argv"]
                added = [argv[i + 1] for i, word in enumerate(argv) if word == "--add-dir"]
                self.assertEqual((self.calls()[-1]["cwd"], added), (cwd, want))
                self.assertFalse({self.work, layout.run} & set(map(os.path.realpath, added)))

    def test_a_deliverable_file_the_agent_wrote_is_saved_to_out(self):
        report = os.path.join(self.agent, "report.md")
        res = self.drive(
            [
                mark("start", "echoing"),
                {"kind": "write", "path": report, "text": "Written.\n"},
                block({**UNSAVED, "deliverable_file": report}),
            ]
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.read(self.out), "Written.\n")
        self.assertEqual(self.last("result")["outcome"]["url"], self.out)


class Outcomes(Integration):
    def test_needs_input(self):
        res = self.drive([block(ASKING)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("delegate: status needs_input", res.stderr)
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(self.last("result")["outcome"]["questions"], ASKING["questions"])

    def test_no_outcome(self):
        res = self.drive(["Thinking."])
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("delegate: the agent run returned no outcome", res.stderr)
        self.assertEqual(self.kinds()[-2:], ["result", "end"])
        self.assertEqual(self.last("result")["error"], "the agent run returned no outcome")
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(len(self.calls()), 1)

    def test_invalid_outcome_then_fixed_by_the_automatic_resume(self):
        res = self.drive([mark("start", "echoing"), block(UNSAVED)], fixes=[block(DONE)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"delegate: invalid outcome: {UNSAVED_ERROR}; resuming once", res.stderr)
        self.assertEqual(self.read(self.out), "Hello.\n")
        first, second = self.calls()
        self.assertEqual(
            (
                option(first, "--session-id"),
                option(second, "--resume"),
                option(second, "--session-id"),
            ),
            (SID, SID, None),
        )
        self.assertEqual(prompt(second).partition("\n\n")[0], FIX.format(error=UNSAVED_ERROR))
        self.assertEqual(
            self.kinds(),
            [
                "session",
                "input",
                "client_session",
                "progress",
                "invalid",
                "session",
                "client_session",
                "result",
                "end",
            ],
        )
        self.assertEqual(self.last("invalid")["error"], UNSAVED_ERROR)
        self.assertEqual(self.last("result")["outcome"]["status"], "done")
        self.assertEqual(res.stderr.count(f"delegate: session {SID}"), 1)

    def test_invalid_outcome_twice(self):
        res = self.drive([block(UNSAVED)])
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn(f"delegate: invalid outcome: {UNSAVED_ERROR}\n", res.stderr)
        self.assertEqual(self.last("result")["error"], f"invalid outcome: {UNSAVED_ERROR}")
        self.assertEqual([option(call, "--resume") for call in self.calls()], [None, SID])
        self.assertFalse(os.path.exists(self.out))

    def test_nonzero_exit_never_reports_success_even_with_an_outcome_block(self):
        res = self.drive([mark("start", "echoing"), block(DONE)], exit=7)
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("delegate: the client's turn failed: error_during_execution", res.stderr)
        self.assertFalse(os.path.exists(self.out))
        saved = self.last("result")
        self.assertEqual(
            (
                saved.get("outcome"),
                saved["error"],
                self.last("end")["rc"],
            ),
            (None, "the client's turn failed: error_during_execution", 7),
        )
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.kinds()[-2:], ["result", "end"])

    def test_nonzero_exit_without_valid_report_still_fails(self):
        for steps in (["Thinking."], [block({**DONE, "status": "maybe"})]):
            with self.subTest(steps=steps):
                before = len(self.calls())
                res = self.drive(steps, exit=7)
                self.assertEqual(res.returncode, 1, res.stderr)
                self.assertIn(
                    "delegate: the client's turn failed: error_during_execution", res.stderr
                )
                self.assertEqual(
                    (self.last("result")["error"], self.last("end")["rc"]),
                    ("the client's turn failed: error_during_execution", 7),
                )
                self.assertNotIn("outcome", self.last("result"))
                self.assertFalse(os.path.exists(self.out))
                self.assertEqual(len(self.calls()), before + 1)


class Stderr(Integration):
    def test_the_clients_stderr_lines_land_in_the_record(self):
        res = self.drive([mark("start", "first"), block(ASKING)])
        self.assertEqual(res.returncode, 0, res.stderr)
        os.unlink(self.transcript)
        res = self.answer([block(DONE)])
        self.assertEqual(res.returncode, 0, res.stderr)
        said = f"No conversation found with session ID: {SID}"
        self.assertEqual([e["text"] for e in self.records() if e["kind"] == "stderr"], [said])
        self.assertEqual(self.kinds()[-4:], ["stderr", "turn_failed", "result", "end"])
        self.assertEqual(
            (self.last("result")["error"], self.last("end")["rc"]), ("the client exited 1", 1)
        )


class NeedsInput(Integration):
    def test_the_answer_resumes_the_session(self):
        res = self.drive([mark("start", "echoing"), block(ASKING)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("delegate: status needs_input", res.stderr)
        self.assertFalse(os.path.exists(self.out), "a deliverable before the answer")
        res = self.answer([block(DONE)], input=ANSWER)
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(os.path.join(Layout(self.work).run, "delegate.json")) as f:
            self.assertEqual(json.load(f)["attempt"], 2)
        records = self.records()
        self.assertEqual(
            self.kinds(),
            [
                "session",
                "input",
                "client_session",
                "progress",
                "result",
                "end",
                "session",
                "input",
                "client_session",
                "result",
                "end",
            ],
        )
        self.assertEqual([e["text"] for e in records if e["kind"] == "input"], ["Hello.", ANSWER])
        self.assertEqual(
            [(e["sid"], e["cwd"]) for e in records if e["kind"] == "session"],
            [(SID, self.agent)] * 2,
        )
        first, second = self.calls()
        self.assertEqual(second["cwd"], self.agent)
        self.assertEqual(
            (
                option(first, "--session-id"),
                option(second, "--resume"),
                option(second, "--session-id"),
            ),
            (SID, SID, None),
        )
        self.assertTrue(prompt(second).startswith(RESUME), "the resume prompt")
        self.assertTrue(
            prompt(second).rpartition("\n\n")[0].endswith(f"\n# Input\n\n{ANSWER}\n"),
            "the answer as the input, before the client's command note",
        )
        self.assertEqual(self.users(), [prompt(first), prompt(second)], "one session's transcript")
        self.assertEqual(
            [*map(bare, records[-2:])],
            [
                {
                    "kind": "result",
                    "outcome": {
                        "status": "done",
                        "title": "hello",
                        "summary": "Hello.",
                        "questions": [],
                        "url": self.out,
                        "files": [],
                    },
                },
                {"kind": "end", "sid": SID, "rc": 0},
            ],
        )
        self.assertEqual(self.read(self.out), DONE["deliverable"])


class RolePicks(Integration):
    ROLE = "researcher"
    PICK = mark("start", "research: a quick, good-enough answer will do")
    REPORT = {
        "status": "done",
        "title": "Greetings",
        "summary": "web",
        "deliverable": "# Greetings\n",
    }

    def test_the_role_picks_and_a_resume_keeps_the_session(self):
        picked = {
            "kind": "progress",
            "name": "start",
            "text": "research: a quick, good-enough answer will do",
        }
        res = self.drive([self.PICK, block(ASKING)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"Progress (start): {picked['text']}", res.stderr)
        self.assertIn(
            "**Your task**: pick it from your charter's Tasks section", prompt(self.calls()[0])
        )
        self.assertEqual([bare(e) for e in self.records() if e["kind"] == "progress"], [picked])
        res = self.answer([block(self.REPORT)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("missing progress mark", res.stderr)
        self.assertNotIn("missing", self.kinds())
        first, second = self.calls()
        self.assertEqual((option(second, "--resume"), option(second, "--session-id")), (SID, None))
        self.assertTrue(prompt(second).startswith(RESUME), "the resume prompt")
        self.assertEqual([bare(e) for e in self.records() if e["kind"] == "progress"], [picked])
        self.assertEqual([e["sid"] for e in self.records() if e["kind"] == "session"], [SID, SID])
        self.assertEqual(self.last("result")["outcome"]["status"], "done")
        self.assertEqual(self.read(self.out), self.REPORT["deliverable"])


class UsageLimit(Integration):
    def test_a_limit_ends_the_run_and_its_session_stays_resumable(self):
        res = self.drive(
            [mark("start", "echoing"), {"kind": "limit", "resets_at": RESETS}, block(DONE)]
        )
        self.assertEqual(res.returncode, 1, res.stderr)
        resets = datetime.fromtimestamp(RESETS, UTC).isoformat(timespec="seconds")
        self.assertIn(f"delegate: usage limit; resets at {resets}\n", res.stderr)
        self.assertNotIn("status done", res.stderr)
        self.assertEqual(
            self.kinds(),
            ["session", "input", "client_session", "progress", "limit", "result", "end"],
        )
        self.assertEqual(self.last("limit")["resets_at"], RESETS)
        self.assertEqual(
            [bare(self.last("result"))],
            [{"kind": "result", "error": f"usage limit; resets at {resets}"}],
        )
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(len(self.calls()), 1)
        session_cls = packaged()
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.config}):
            self.assertEqual(
                session_cls.inspect(
                    Spec(
                        workdir=Layout(self.work).run,
                        access=Access(cwd=self.agent),
                        client="claude",
                        sid=SID,
                    )
                ).resumable,
                "yes",
            )


class ConfigErrors(Integration):
    def test_cwd_in_run_dir_writes_nothing(self):
        work = os.path.join(self.root, "new")
        run = Layout(work).run
        for cwd in (work, run):
            with self.subTest(cwd=cwd):
                argv = [*RUN, "--role", self.ROLE, "--input", "x", "--workdir", work]
                res = self.drive([block(DONE)], argv=[*argv, "--sid", SID, "--cwd", cwd])
                self.assertEqual((res.returncode, res.stdout), (2, ""), res.stderr)
                self.assertIn(f"cwd {cwd} overlaps the run dir {run}", res.stderr)
                self.assertFalse(os.path.lexists(work))
                self.assertEqual(self.calls(), [])


class Draft(Integration):
    def holds(self, text: str) -> bool:
        try:
            return self.read(self.out) == text
        except OSError:
            return False

    def inputs(self) -> int:
        try:
            return self.kinds().count("input")
        except ValueError:  # a line half appended
            return 0

    def test_an_answer_keeps_the_draft_until_the_deliverable_replaces_it(self):
        result = self.drive(
            [
                mark("start", "echoing"),
                {"kind": "write", "path": self.out, "text": DRAFT},
                block(ASKING),
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.holds(DRAFT), "the draft while waiting for an answer")
        gate = os.path.join(self.root, "gate")
        result = self.answer([block(DONE)], wait=False, gate=gate)
        self.assertEqual(result.returncode, 0, result.stderr)
        live_tmux.wait(lambda: self.inputs() == 2, timeout=WAIT, what="the answer input")
        self.assertTrue(self.holds(DRAFT), "the draft as the resumed client starts")
        open(gate, "w").close()
        self.completed()
        self.assertEqual(option(self.calls()[1], "--resume"), SID)
        self.assertEqual(self.last("result")["outcome"]["status"], "done")
        self.assertEqual(self.read(self.out), DONE["deliverable"])


class Interrupt(Integration):
    def started(self) -> bool:
        try:
            return "start" in [e.get("name") for e in self.records() if e["kind"] == "progress"]
        except (OSError, ValueError):
            return False

    def test_interrupt_kills_a_hung_agent(self):
        self.scene([mark("start", "waiting")], hang=True)
        proc = self.popen()
        self.until(proc, self.started, "reported the start")
        (call,) = self.calls()
        os.kill(proc.pid, signal.SIGINT)
        proc.wait(timeout=WAIT)
        # Before communicate(): the fake shares the CLI's stderr pipe, which would wait for it to
        # die by any path.
        self.assertFalse(running(call["pid"]))
        out, err = proc.communicate()
        self.assertIn(proc.returncode, (-signal.SIGINT, 130), out + err)
        result, end = map(bare, self.records()[-2:])
        self.assertEqual(result, {"kind": "result", "error": "stopped: KeyboardInterrupt"})
        self.assertEqual(end, {"kind": "end", "sid": SID, "rc": 1})


if __name__ == "__main__":
    unittest.main()
