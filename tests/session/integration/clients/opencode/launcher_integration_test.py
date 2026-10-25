"""The opencode launcher as a process: prompt on stdin, output copied, the session watchdog."""

import json
import subprocess
import sys
import time
import unittest

from ensemble.session.clients.opencode import launcher
from tests.session.case import Case
from tests.session.integration.clients.opencode import launcher_child

LAUNCHER = (sys.executable, "-I", launcher.__file__)
BOUND = launcher_child.BOUND


def child(mode: str) -> tuple[str, ...]:
    return (sys.executable, "-I", launcher_child.__file__, mode)


def run(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        (*LAUNCHER, *args),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


class LauncherTest(Case):
    def test_the_prompt_reaches_stdin_and_output_and_exit_code_pass_through(self):
        result = run("--timeout", "30", "--prompt", "Hi there\n- a list", "--", *child("echo"))
        self.assertEqual(result.returncode, 3)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], BOUND)
        self.assertEqual(json.loads(lines[1]), {"text": "Hi there\n- a list"})
        self.assertEqual(result.stderr, "oops\n")

    def test_a_run_without_a_session_is_killed(self):
        started = time.monotonic()
        result = run("--timeout", "1", "--prompt", "", "--", *child("silent"))
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(result.returncode, 124)
        self.assertEqual(result.stdout, '{"type": "text"}\n')
        self.assertEqual(result.stderr, "opencode: no session after 1 s; stopped\n")

    def test_a_descendant_holding_the_pipes_does_not_keep_the_run(self):
        started = time.monotonic()
        result = run("--timeout", "1", "--prompt", "", "--", *child("spawn"))
        self.assertEqual(result.returncode, 124)
        self.assertLess(time.monotonic() - started, 8)

    def test_a_bound_run_outlives_the_timeout(self):
        result = run("--timeout", "1", "--prompt", "", "--", *child("bound"))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.splitlines(), [BOUND, "done"])

    def test_a_missing_command(self):
        result = run("--timeout", "1", "--prompt", "", "--", "/nonexistent/opencode", "run")
        self.assertEqual(result.returncode, 127)
        self.assertEqual(result.stderr, "opencode: command not found: /nonexistent/opencode\n")

    def test_bad_usage(self):
        for args in ((), ("--timeout", "x", "--prompt", "", "--", "a"), ("--timeout", "1")):
            with self.subTest(args=args):
                result = run(*args)
                self.assertEqual(result.returncode, 2)
                self.assertTrue(result.stderr.startswith("usage: "))


if __name__ == "__main__":
    unittest.main()
