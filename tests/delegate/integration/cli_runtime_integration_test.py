"""Real process behavior in isolated integration fixtures."""

import json
import os
import shutil
import subprocess
from datetime import datetime
from unittest import mock

from ensemble.delegate import Outcome
from ensemble.delegate.composer import Composer
from ensemble.delegate.engine.types import Result
from tests.delegate.case import PY, child_env
from tests.delegate.cli_case import Base
from tests.delegate.replies import block
from tests.delegate.unit.runs import DONE, START, limit, said


class Exits(Base):
    def test_exit_codes(self):
        at = 1767225600.0
        when = datetime.fromtimestamp(at).astimezone().isoformat(timespec="seconds")
        cases = [
            (
                "a report followed by exit 7",
                [said(START, block(DONE)), {"exit": 7}],
                1,
                "delegate: the client exited 7",
            ),
            (
                "needs_input",
                [said(START, block({**DONE, "status": "needs_input", "questions": ["Q?"]}))],
                0,
                "delegate: status needs_input",
            ),
            ("no outcome", [said(START)], 1, "delegate: the agent run returned no outcome"),
            (
                "invalid twice",
                [said(START, block({**DONE, "status": "maybe"}))],
                1,
                "delegate: invalid outcome: status 'maybe' is not one of done, needs_input, failed",
            ),
            (
                "no outcome, the client exited 5",
                [said(START), {"exit": 5}],
                1,
                "delegate: the client exited 5",
            ),
            (
                "a usage limit",
                [said(START), limit(at), {"sleep": 30}],
                1,
                f"delegate: usage limit; resets at {when}",
            ),
            (
                "an unknown reset time",
                [said(START), limit(None), {"sleep": 30}],
                1,
                "delegate: usage limit; reset time unknown",
            ),
        ]
        for name, steps, want, last in cases:
            with self.subTest(name):
                shutil.rmtree(self.work, ignore_errors=True)
                self.steps(*steps)
                code, out, err = self.runs()
                self.assertEqual((code, out), (want, ""), err)
                self.assertTrue(err.endswith(f"\n{last}\n"), err)

    def test_a_client_that_cannot_start_exits_1(self):
        with mock.patch(
            "subprocess.Popen", side_effect=FileNotFoundError(2, "No such file or directory", "x")
        ):
            code, out, err = self.runs()
        self.assertEqual((code, out), (1, ""))
        self.assertRegex(err, r"(?m)^delegate: headless: worker headless-\S+: cannot start guard: ")

    def test_agent_text_is_printed_printable(self):
        for result, want, line in (
            (
                Result(0, None, "invalid outcome: a\x1b[2Jb\x07"),
                1,
                "delegate: invalid outcome: a[2Jb",
            ),
            (
                Result(9, Outcome("done", "t", "s"), "the client exited 9"),
                0,
                "delegate: status done",
            ),
        ):
            with self.subTest(want=want):
                self.ran = result
                code, out, err = self.runs()
                self.assertEqual((code, out, err.splitlines()[-1]), (want, "", line))


class Deliverable(Base):
    """--out PATH: a stderr line naming the file holding the deliverable; --out - is refused."""

    def test_out_path_is_named_on_stderr_holding_the_deliverable(self):
        code, out, err = self.runs(out=os.path.join("work", "out.md"))
        self.assertEqual((code, out), (0, ""), err)
        self.assertTrue(
            err.endswith(f"\ndelegate: deliverable {self.out}\ndelegate: status done\n"), err
        )

    def test_no_path_line_without_a_deliverable_at_out(self):
        asking = {**DONE, "status": "needs_input", "questions": ["Q?"], "deliverable": ""}
        self.steps(said(START, block(asking)))
        code, out, err = self.runs()
        self.assertEqual((code, out), (0, ""), err)
        self.assertNotIn("delegate: deliverable", err)
        elsewhere = Result(0, Outcome("done", "t", "s", deliverable="# Doc\n"))
        self.ran = elsewhere
        code, out, err = self.runs()
        self.assertEqual((code, out, err.splitlines()[-1]), (0, "", "delegate: status done"))
        self.assertNotIn("delegate: deliverable", err)


class Input(Base):
    def test_input_is_text_from_the_argument_or_stdin_never_a_files_content(self):
        with open(os.path.join(self.tmp, "input.md"), "w") as f:
            f.write("SECRET")
        for given, stdin, want in (("input.md", "", "input.md"), ("-", "Use B.\n", "Use B.\n")):
            with self.subTest(given=given):
                code, _, err = self.runs(input=given, stdin=stdin)
                self.assertEqual(code, 0, err)
                inputs = [r["text"] for r in self.records() if r["kind"] == "input"]
                prompt = self.launches()[-1]["argv"][-1]
                self.assertEqual(inputs[-1], want)
                self.assertTrue(prompt.endswith(f"\n# Input\n\n{want.strip()}\n"), prompt[-100:])
                self.assertNotIn("SECRET", prompt)


class DryRun(Base):
    def test_the_client_defaults_to_the_roles_in_the_config(self):
        code, out, _ = self.runs("--dry-run", client=None)
        self.assertEqual(
            (code, json.loads(out)["client"]), (0, Composer().resolve("dummy-tester").client)
        )
        self.local('client = "fake"\n')
        code, _, err = self.runs(client=None)
        self.assertEqual((code, len(self.launches())), (0, 1), err)


class List(Base):
    def test_it_runs_as_a_module_from_the_import_root(self):
        done = subprocess.run(
            [*PY, "-m", "ensemble.delegate.cli", "run", "--list"],
            capture_output=True,
            text=True,
            env=child_env(),
            cwd=self.tmp,
            timeout=60,
        )
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(done.stdout, self.cli("run", "--list")[1])
