"""Test supervision in isolated child interpreters."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from runner_fixtures import HERE, SCRIPT

from tests.case import Case
from tests.utils.sandbox import clean_env


class Supervise(Case):
    def supervise(self, mode):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / "job.py").write_text(SCRIPT.format(github=str(HERE), mode=mode))
        env = {
            k: v
            for k, v in clean_env(None, str(HERE.parent), str(HERE)).items()
            if k != "RUN_TESTS_STATE"
        }
        env["GITHUB_STEP_SUMMARY"] = str(tmp / "summary.md")
        env["RUN_TESTS_DURATIONS"] = str(tmp / "durations.jsonl")
        proc = subprocess.run(
            [sys.executable, str(tmp / "job.py")], env=env, capture_output=True, text=True
        )
        summary = tmp / "summary.md"
        return proc, summary.read_text() if summary.exists() else ""

    def test_a_crash_after_all_tests_pass_is_annotated_with_the_last_test_and_stack(self):
        proc, summary = self.supervise("after")
        self.assertEqual(proc.returncode, 134)
        message = "interpreter crashed after all tests passed (exit 134); last test started: "
        self.assertIn(f"::error title=Unit x::{message}__main__.T.test_a%0A", proc.stdout)
        self.assertIn("Fatal Python error: Aborted", proc.stdout)
        self.assertIn(message, summary)

    def test_a_crash_in_a_test_names_that_test(self):
        proc, _ = self.supervise("mid")
        self.assertEqual(proc.returncode, 134)
        self.assertIn(
            "::error title=Unit x::interpreter crashed (exit 134) in __main__.T.test_a", proc.stdout
        )
        self.assertIn("in test_a", proc.stdout)

    def test_an_exit_before_any_test_is_annotated(self):
        proc, _ = self.supervise("early")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("::error title=Unit x::exited 2 before any test started", proc.stdout)

    def test_a_plain_failure_or_success_adds_no_crash_annotation(self):
        for mode, code, annotations in (("fail", 1, 1), ("ok", 0, 0)):
            with self.subTest(mode=mode):
                proc, _ = self.supervise(mode)
                self.assertEqual(proc.returncode, code)
                self.assertEqual(proc.stdout.count("::error"), annotations)
                self.assertNotIn("crashed", proc.stdout)

    def test_a_process_outliving_the_run_fails_it_and_is_killed(self):
        proc, summary = self.supervise("leak")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        pid = re.search(r"^started (\d+)$", proc.stdout, re.MULTILINE).group(1)
        self.assertIn(f"::error title=Unit x::leaked after the run: pid {pid}: ", proc.stdout)
        self.assertIn(f"### Unit x: leaked processes\n\n- pid {pid}: ", summary)
        deadline = time.monotonic() + 3
        while True:
            state = subprocess.run(["ps", "-p", pid, "-o", "stat="], capture_output=True, text=True)
            if state.returncode == 1 or state.stdout.strip().startswith("Z"):
                break
            self.assertLess(time.monotonic(), deadline, f"leaked process {pid} remains")
            time.sleep(0.05)

    @unittest.skipUnless(shutil.which("tmux"), "tmux is unavailable")
    def test_a_tmux_server_outliving_the_run_fails_it(self):
        proc, summary = self.supervise("tmux")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertRegex(
            proc.stdout,
            r"::error title=Unit x::leaked after the run: tmux server \S+/tmux-\d+/default\n",
        )
        self.assertIn("### Unit x: leaked processes\n\n- tmux server ", summary)

    def test_a_process_ending_inside_the_grace_is_not_a_leak(self):
        proc, summary = self.supervise("brief")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("::error", proc.stdout)
        self.assertNotIn("leaked", summary)

    def main(self, source, **extra):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / "pkg").mkdir()
        (tmp / "pkg" / "__init__.py").write_text("")
        (tmp / "pkg" / "x_test.py").write_text(source)
        env = {
            k: v
            for k, v in clean_env(None, str(HERE.parent), str(HERE)).items()
            if k not in ("RUN_TESTS_STATE", "GITHUB_STEP_SUMMARY", "RUN_TESTS_COVERAGE")
        }
        env["RUN_TESTS_DURATIONS"] = str(tmp / "durations.jsonl")
        proc = subprocess.run(
            [sys.executable, str(HERE / "run_tests.py"), "Unit pkg", "pkg"],
            cwd=tmp,
            env={**env, **extra},
            capture_output=True,
            text=True,
        )
        return tmp, proc

    def test_main_discovers_from_the_cwd(self):
        _, proc = self.main(
            "import unittest\n\n\nclass X(unittest.TestCase):\n    def test_x(self):\n        self.fail('no')\n"
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "::error title=pkg.x_test.X.test_x::AssertionError: no\n")

    def test_run_tests_coverage_saves_the_child_process_src_arcs_outside_the_checkout(self):
        import coverage

        source = (
            "import unittest\n\nfrom ensemble.utils.text import sid_error\n\n\n"
            "class X(unittest.TestCase):\n    def test_x(self):\n"
            "        self.assertIsNone(sid_error('ok'))\n"
        )
        out = Path(self.enterContext(tempfile.TemporaryDirectory()))
        tmp, proc = self.main(source, RUN_TESTS_COVERAGE=str(out / "data"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = coverage.CoverageData(basename=str(out / "data"))
        data.read()
        text = str(HERE.parent / "src/ensemble/utils/text.py")
        self.assertIn(text, data.measured_files())
        self.assertIn((10, 11), data.arcs(text))
        self.assertFalse(any(name.startswith(".coverage") for name in os.listdir(tmp)))
        self.assertFalse(any(name.startswith(".coverage") for name in os.listdir(HERE.parent)))

    def test_without_run_tests_coverage_the_child_never_imports_coverage(self):
        source = (
            "import sys, unittest\n\n\nclass X(unittest.TestCase):\n    def test_x(self):\n"
            "        self.assertNotIn('coverage', sys.modules)\n"
        )
        tmp, proc = self.main(source)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sorted(os.listdir(tmp)), ["durations.jsonl", "pkg"])
