"""ensemble-delegate's queries, each a fresh process, on a workdir a foreground run left."""

import json
import os
import subprocess

from ensemble.utils import Record, Status

from delegate.tests.case import PY
from delegate.tests.integration.cli import DONE, SID, Integration
from delegate.tests.replies import block, mark

CLI = (*PY, "-m", "ensemble.delegate.cli")
NAME = f"dummy-tester-{SID[:8]}"


class Queries(Integration):
    def setUp(self):
        super().setUp()
        res = self.drive([mark("start", "echoing"), block(DONE)])
        self.assertEqual(res.returncode, 0, res.stderr)

    def query(self, *argv, stdin=""):
        res = subprocess.run(
            (*CLI, *argv),
            cwd=self.proj,
            env=self.env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return res.returncode, res.stdout, res.stderr

    def test_status_and_outcome_answer_from_the_workdir_or_its_record(self):
        line = (
            Status(
                name=NAME,
                alive=False,
                state="gone",
                outcome="done",
                attach=None,
                cwd=self.agent,
                hidden=False,
            ).to_line()
            + "\n"
        )
        self.assertEqual(self.query("status", self.work), (0, line, ""))
        resume = ("ensemble-delegate", "resume", self.work)
        record = Record(
            1, "role", NAME, (), SID, self.work, ("ensemble-delegate",), resume, None, None
        )
        self.assertEqual(self.query("status", stdin=record.to_line() + "\n"), (0, line, ""))
        code, out, err = self.query("outcome", self.work)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {**DONE, "questions": [], "url": self.out, "files": []})

    def test_a_missing_workdir_and_a_repeated_name_exit_2(self):
        missing = os.path.join(self.root, "missing")
        code, out, err = self.query("status", missing, self.work)
        self.assertEqual(
            (code, out),
            (
                2,
                Status(
                    name=NAME,
                    alive=False,
                    state="gone",
                    outcome="done",
                    attach=None,
                    cwd=self.agent,
                    hidden=False,
                ).to_line()
                + "\n",
            ),
        )
        self.assertTrue(err.startswith(f"delegate: {missing}: "), err)
        self.assertEqual(
            self.query("status", self.work, self.work),
            (2, "", f"delegate: name {NAME} given more than once\n"),
        )

    def test_close_twice_succeeds_and_nothing_is_showable(self):
        self.assertEqual(self.query("close", self.work), (0, "", ""))
        self.assertEqual(self.query("close", self.work), (0, "", ""))
        self.assertEqual(
            self.query("show", self.work),
            (1, "", f"delegate: {self.work}: delegate has no live showable agent session\n"),
        )
        self.assertEqual(
            self.query("attach-command", self.work),
            (1, "", f"delegate: {self.work}: no showable session\n"),
        )
