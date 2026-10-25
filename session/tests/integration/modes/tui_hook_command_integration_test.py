"""`ensemble-session hook DIR LABEL` (FR-32), the command every client's hook config runs: its usage
and a closed stdin. tui_hook_integration_test runs the hook's other cases through it (HookBase).
"""

import os
import subprocess
import sys
import unittest

from session.tests.tui_hook_support import COMMAND, HookBase


class UsageThroughCommand(HookBase):
    def test_a_wrong_argument_count_is_one_usage_line_and_exits_0_writing_nothing(self):
        for args in ([], [self.dir], [self.dir, "stop", "x"]):
            with self.subTest(args=args):
                self.assertError(self.hook(None, args=args), "usage: ensemble-session hook")
        self.assertNothingWritten()

    def test_a_closed_stdin_is_one_stderr_line_and_exits_0_writing_nothing(self):
        res = subprocess.run(
            [COMMAND, "hook", self.dir, "tool"],
            capture_output=True,
            env={**self.env, "PATH": f"{os.path.dirname(sys.executable)}:/usr/bin:/bin"},
            timeout=60,
            preexec_fn=lambda: os.close(0),
        )
        self.assertEqual((res.returncode, res.stdout), (0, b""), res.stderr)
        self.assertError(res.stderr.decode(), "Error")
        self.assertNothingWritten()


if __name__ == "__main__":
    unittest.main()
