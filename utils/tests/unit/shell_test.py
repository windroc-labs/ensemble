"""utils/src/ensemble/utils/shell.py: err_text, redact and command_path."""

import io
import os
import signal
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ensemble.utils import (
    STOP_WAIT,
    command_path,
    kill_group,
    over_argv,
    probe,
    resume_command,
    shell,
)

from utils.tests.case import Case
from utils.tests.process_fixtures import DEVNULL, TimeoutExpired


class ShellExecution(Case):
    def test_explicit_environment_is_forwarded_without_inheriting_values(self):
        env = {"PATH": "/custom/bin", "ONLY": "1"}
        with mock.patch("subprocess.run", return_value="result") as run:
            self.assertEqual(shell.sh(["git", "status"], 5, env=env), "result")
        run.assert_called_once_with(
            ["git", "status"],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=shell.subprocess.DEVNULL,
            env=env,
        )


class CommandPath(Case):
    def test_the_first_path_entry_holding_the_command_else_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = (os.path.join(os.path.realpath(tmp), d) for d in ("a b", "c"))
            for folder in (first, second):
                os.mkdir(folder)
                path = os.path.join(folder, "tool")
                with open(path, "w", encoding="utf-8") as f:
                    f.write("#!/bin/sh\n")
                os.chmod(path, 0o755)
            with mock.patch.dict(
                os.environ, {"PATH": os.pathsep.join(("/nowhere", first, second))}
            ):
                self.assertEqual(command_path("tool"), os.path.join(first, "tool"))
                self.assertIsNone(command_path("absent-tool"))


class KillGroup(Case):
    def test_signals_the_group_until_closed_pipes_and_a_reaped_process_confirm_exit(self):
        for ended, returncode, signals in (
            (False, None, True),
            (True, None, True),
            (False, 0, True),
            (True, 0, False),
        ):
            with self.subTest(ended=ended, returncode=returncode):
                proc = mock.Mock(pid=4242, returncode=returncode)
                with mock.patch("os.killpg") as killpg:
                    kill_group(proc, ended=ended, wait=0.25)
                self.assertEqual(
                    killpg.call_args_list, [mock.call(4242, signal.SIGKILL)] if signals else []
                )
                proc.kill.assert_not_called()
                proc.wait.assert_called_once_with(timeout=0.25)

    def test_signal_failure_falls_back_to_the_child_and_bounds_the_wait(self):
        for error in (ProcessLookupError(), PermissionError()):
            with self.subTest(error=type(error).__name__):
                proc = mock.Mock(pid=4242, returncode=None)
                proc.wait.side_effect = TimeoutExpired("child", STOP_WAIT)
                with mock.patch("os.killpg", side_effect=error):
                    kill_group(proc)
                proc.kill.assert_called_once_with()
                proc.wait.assert_called_once_with(timeout=STOP_WAIT)

    def test_a_child_already_gone_still_gets_reaped(self):
        proc = mock.Mock(pid=4242, returncode=None)
        proc.kill.side_effect = ProcessLookupError()
        with mock.patch("os.killpg", side_effect=ProcessLookupError()):
            kill_group(proc)
        proc.wait.assert_called_once_with(timeout=STOP_WAIT)


class Probe(Case):
    def test_returns_stdout_and_closes_its_pipe_with_an_explicit_environment(self):
        proc = mock.Mock(stdout=io.StringIO(), communicate=mock.Mock(return_value=("output", None)))
        env = {"ONLY": "value"}
        with mock.patch("subprocess.Popen", return_value=proc) as popen:
            self.assertEqual(probe(["tool"], cwd="/work", env=env, timeout=3), "output")
        popen.assert_called_once_with(
            ["tool"],
            cwd="/work",
            env=env,
            stdin=DEVNULL,
            stdout=shell.subprocess.PIPE,
            stderr=DEVNULL,
            text=True,
            errors="replace",
            process_group=0,
        )
        proc.communicate.assert_called_once_with(timeout=3)
        proc.wait.assert_not_called()
        self.assertTrue(proc.stdout.closed)

    def test_spawn_errors_return_none_without_signalling(self):
        for error in (OSError("missing"), ValueError("bad argument")):
            with (
                self.subTest(error=error),
                mock.patch("subprocess.Popen", side_effect=error),
                mock.patch("os.killpg") as killpg,
            ):
                self.assertIsNone(probe(["tool"], cwd="/work", env={}, timeout=3))
                killpg.assert_not_called()

    def test_timeout_kills_and_reaps_the_group_then_closes_stdout(self):
        proc = mock.Mock(pid=4242, returncode=None, stdout=io.StringIO())
        proc.communicate.side_effect = TimeoutExpired("tool", 3)
        with mock.patch("subprocess.Popen", return_value=proc), mock.patch("os.killpg") as killpg:
            self.assertIsNone(probe(["tool"], cwd="/work", env={}, timeout=3))
        killpg.assert_called_once_with(4242, signal.SIGKILL)
        proc.wait.assert_called_once_with(timeout=STOP_WAIT)
        self.assertTrue(proc.stdout.closed)


class OverArgv(Case):
    def test_measures_the_json_encoded_utf_8_bytes(self):
        for text, want in (
            ("a" * (shell.PROMPT_ARGV_MAX - 2), False),
            ("a" * (shell.PROMPT_ARGV_MAX - 1), True),
            ("\n" * (shell.PROMPT_ARGV_MAX // 2 - 1), False),
            ("\n" * (shell.PROMPT_ARGV_MAX // 2), True),
            ('"' * 60_000, True),
            ("雪" * 33_332, False),
            ("雪" * 33_333, True),
            ("", False),
        ):
            with self.subTest(size=len(text), head=text[:1]):
                self.assertIs(over_argv(text), want)


class ErrText(Case):
    def test_userinfo_is_redacted(self):
        res = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="fatal: could not read Password for 'https://SECRETTOK@github.com': no\n"
            "fatal: unable to access 'https://u:SECRET2@ghe.io/o/n/'",
        )
        text = shell.err_text(res)
        self.assertNotIn("SECRET", text)
        self.assertIn("'https://***@github.com'", text)
        self.assertIn("'https://***@ghe.io/o/n/'", text)

    def test_trimmed_and_capped(self):
        for stderr, want in (
            (None, ""),
            ("", ""),
            ("  HTTP 404: Not Found \n", "HTTP 404: Not Found"),
            ("x" * 300, "x" * 200),
        ):
            with self.subTest(stderr=stderr):
                self.assertEqual(shell.err_text(SimpleNamespace(returncode=1, stderr=stderr)), want)


class ResumeCommand(Case):
    def test_builds_a_shell_safe_resume_command(self):
        for workdir, env, argv, want in (
            ("/work", {}, ("codex", "resume", "id"), "cd /work && codex resume id"),
            (
                "/work dir",
                {"LABEL": "a b", "EMPTY": ""},
                ("claude", "--resume", "a; echo bad", ""),
                "cd '/work dir' && LABEL='a b' EMPTY='' claude --resume 'a; echo bad' ''",
            ),
            (
                "/w",
                {"A-B": "a b", "X;Y": "$(bad)", "-x": ""},
                ("tool",),
                "cd /w && env -- 'A-B=a b' 'X;Y=$(bad)' -x= tool",
            ),
        ):
            with self.subTest(workdir=workdir):
                self.assertEqual(resume_command(workdir, env, argv), want)


if __name__ == "__main__":
    unittest.main()
