"""hosts/docker/daemon.py: the capped run and the docker exec runner, over fake processes."""

import contextlib
import os
import threading
from types import SimpleNamespace
from unittest import mock

from ensemble.session.engine import tmux_cli
from ensemble.session.hosts.docker import daemon
from ensemble.session.hosts.docker.daemon import OUT_MAX, Overflow, capped, exec_runner
from tests.session.case import Case
from tests.session.unit.hosts.docker.support import FakePopen, FakeProc
from tests.utils.process_fixtures import DEVNULL, PIPE, TimeoutExpired


class CappedTest(Case):
    def test_returns_text_and_feeds_input_from_a_file(self):
        popen = FakePopen((b"out\xff", b"err", 3))
        with mock.patch("subprocess.Popen", popen):
            res = capped(["x"], {"A": "1"}, input="hi")
        self.assertEqual((res.returncode, res.stdout, res.stderr), (3, "out�", "err"))
        ((argv, kw, given),) = popen.calls
        self.assertEqual(
            (argv, kw["env"], given, kw["stdout"], kw["stderr"]),
            (["x"], {"A": "1"}, b"hi", PIPE, PIPE),
        )
        with mock.patch("subprocess.Popen", FakePopen((b"", b"", 0))) as popen:
            capped(["x"], {})
        self.assertEqual(popen.calls[0][2], DEVNULL)

    def test_a_silent_process_times_out_and_is_killed(self):
        proc = FakeProc(["x"], hang=True)
        with mock.patch("subprocess.Popen", return_value=proc), self.assertRaises(TimeoutExpired):
            capped(["x"], {}, timeout=0.1)
        self.assertTrue(proc.killed)

    def test_output_over_the_cap_is_killed(self):
        proc = FakeProc(["x"], hang=True)
        write = proc.fds[0]

        def flood():
            with contextlib.suppress(OSError):
                os.write(write, b"x" * (OUT_MAX + 1))

        thread = threading.Thread(target=flood)
        thread.start()
        with (
            mock.patch("subprocess.Popen", return_value=proc),
            self.assertRaisesRegex(Overflow, "output over"),
        ):
            capped(["x"], {}, timeout=30)
        thread.join()
        self.assertTrue(proc.killed)


class ExecRunnerTest(Case):
    def setUp(self):
        patcher = mock.patch("pwd.getpwuid", return_value=SimpleNamespace(pw_dir="/Users/a"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def runner(self, *results):
        self.calls = []

        def run(argv, _env, *, input=None, timeout):
            self.calls.append((argv, input, timeout))
            result = results[len(self.calls) - 1]
            if isinstance(result, BaseException):
                raise result
            return SimpleNamespace(returncode=result[0], stdout="", stderr=result[1])

        return exec_runner("/bin/docker", "c1", run)

    def test_runs_inside_the_container_within_the_timeout(self):
        res = self.runner((0, ""))(["tmux", "ls"], input="x", timeout=60, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(
            self.calls,
            [
                (
                    [
                        "/bin/docker",
                        "--host",
                        "unix:///Users/a/.docker/run/docker.sock",
                        "exec",
                        "-i",
                        "c1",
                        "tmux",
                        "ls",
                    ],
                    "x",
                    daemon.RUN_TIMEOUT,
                )
            ],
        )

    def test_a_gone_or_stopped_container_reads_as_no_server(self):
        for result in (
            (1, "Error response from daemon: No such container: c1\n"),
            (1, "Error response from daemon: container c1 is not running"),
        ):
            with self.subTest(result=result):
                res = self.runner(result)(["tmux", "ls"])
                self.assertEqual(res.returncode, 1)
                self.assertTrue(res.stderr.startswith("no server running on c1: "))
                self.assertTrue(tmux_cli.absent(res))

    def test_a_timeout_or_overflow_is_a_failed_call_not_an_absent_server(self):
        for result in (TimeoutExpired("docker", 10), Overflow("output over 1 bytes")):
            with self.subTest(result=result):
                res = self.runner(result)(["tmux", "ls"])
                self.assertEqual((res.returncode, res.stdout), (1, ""))
                self.assertTrue(res.stderr.startswith("docker exec in c1: "))
                self.assertFalse(tmux_cli.absent(res))

    def test_other_failures_pass_through(self):
        res = self.runner((1, "can't find session: =w"))(["tmux", "ls"])
        self.assertEqual(res.stderr, "can't find session: =w")
