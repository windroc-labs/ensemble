"""transcript.py run as the manager runs it, `python3 -I <script>`, over a fake opencode.db."""

import json
import os
import subprocess
import sys

from ensemble.session.clients.opencode import transcript
from tests.session.case import Case
from tests.session.unit.clients.opencode.support import SES, make_db, text
from tests.utils.sandbox import scratch


class TranscriptScript(Case):
    def setUp(self):
        self.data = scratch(self)
        self.db = os.path.join(self.data, "opencode", "opencode.db")

    def run_script(self, *argv):
        env = {"PATH": os.environ.get("PATH", ""), "HOME": self.data, "XDG_DATA_HOME": self.data}
        return subprocess.run(
            [sys.executable, "-I", transcript.__file__, *argv],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_a_sent_text_is_found_and_an_unsent_one_is_not(self):
        make_db(
            self.db,
            {SES: 2},
            [(SES, "user", 1, [text("Please rebase.")]), (SES, "assistant", 2, [text("Rebased.")])],
        )
        result = self.run_script(SES)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(
            [(r["role"], r["text"]) for r in rows],
            [
                ("user", "Please rebase."),
                ("assistant", "Rebased."),
            ],
        )
        self.assertNotIn("Merge it.", result.stdout)

    def test_a_missing_db_is_one_error_line(self):
        result = self.run_script(SES, "--db", self.db)
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertRegex(result.stderr, r"\Aopencode transcript: no database .*\n\Z")
