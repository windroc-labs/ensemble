"""The Codex app-server client's answer matching, line bounds and process cleanup; the rate-limit
probe.
"""

import contextlib
import io
from unittest import mock

from ensemble.session.clients.codex.common import app_server
from ensemble.session.clients.codex.tui import start as tui_start
from tests.session.case import Case


class FakeTermination(app_server.Termination):
    def __init__(self, signum):
        self.signum = signum
        self.checks = mock.Mock()

    def check(self) -> None:
        self.checks()


class TerminationTest(Case):
    def test_incomplete_implementation_cannot_be_instantiated(self):
        class Incomplete(app_server.Termination):
            signum = 0

        with self.assertRaisesRegex(TypeError, "abstract.*check"):
            Incomplete()

    def test_startup_implementation_preserves_the_first_received_signal(self):
        termination = tui_start._Termination()
        self.assertIsInstance(termination, app_server.Termination)
        self.assertEqual(termination.signum, 0)
        self.assertIsNone(termination.check())
        termination.receive(tui_start.signal.SIGTERM, None)
        termination.receive(tui_start.signal.SIGHUP, None)
        with self.assertRaises(tui_start.Stopped) as stopped:
            termination.check()
        self.assertEqual(termination.signum, tui_start.signal.SIGTERM)
        self.assertEqual(stopped.exception.signum, tui_start.signal.SIGTERM)


class ClientTest(Case):
    def test_answer_ignores_notifications_wrong_ids_and_malformed_json(self):
        replies = [
            b'{"id":1,"result":{}}',
            b"not json",
            b'{"id":true,"result":1}',
            b'{"method":"event","params":{}}',
            b'{"id":1,"result":{}}',
            b'{"id":2,"result":{"data":[]}}',
        ]
        proc = mock.Mock()
        proc.stdout.fileno.return_value = 42
        with (
            mock.patch("subprocess.Popen", return_value=proc),
            mock.patch("select.select", return_value=([42], [], [])),
            mock.patch("os.read", side_effect=[b"\n".join(replies) + b"\n", b""]),
            mock.patch("os.killpg"),
        ):
            self.assertEqual(
                app_server.AppServer.call("fake", [], "hooks/list", env={}, wait=1, limit=1024),
                {"data": []},
            )
        with (
            mock.patch("subprocess.Popen", return_value=proc),
            mock.patch("select.select", return_value=([42], [], [])),
            mock.patch(
                "os.read",
                side_effect=[
                    b'{"id":1,"result":{}}\n{"id":2,"error":{"message":"denied"}}\n',
                    b"",
                ],
            ),
            mock.patch("os.killpg"),
            self.assertRaisesRegex(app_server.Error, "denied"),
        ):
            app_server.AppServer.call("fake", [], "hooks/list", env={}, wait=1, limit=1024)

    def test_termination_before_spawn_still_checks_on_exit(self):
        termination = FakeTermination(15)
        termination.checks.side_effect = RuntimeError("terminated")
        with (
            mock.patch("subprocess.Popen") as popen,
            mock.patch("os.killpg") as kill,
            self.assertRaisesRegex(RuntimeError, "terminated"),
        ):
            app_server.AppServer.call(
                "fake", [], "hooks/list", env={}, wait=1, limit=1024, termination=termination
            )
        popen.assert_not_called()
        kill.assert_not_called()
        self.assertEqual(termination.checks.call_count, 2)

    def test_termination_after_spawn_closes_before_final_check(self):
        proc = mock.Mock(pid=123)
        proc.stdout.fileno.return_value = 42
        termination = FakeTermination(15)

        def check():
            if termination.checks.call_count == 1:
                return
            if termination.checks.call_count == 3:
                proc.wait.assert_called_once_with()
                proc.stdin.close.assert_called_once_with()
                proc.stdout.close.assert_called_once_with()
            raise RuntimeError("terminated")

        termination.checks.side_effect = check
        with (
            mock.patch("subprocess.Popen", return_value=proc),
            mock.patch("os.killpg") as kill,
            self.assertRaisesRegex(RuntimeError, "terminated"),
        ):
            app_server.AppServer.call(
                "fake", [], "hooks/list", env={}, wait=1, limit=1024, termination=termination
            )
        kill.assert_called_once_with(123, app_server.signal.SIGKILL)
        proc.stdin.write.assert_not_called()
        self.assertEqual(termination.checks.call_count, 3)

    def test_spawn_failure_still_checks_termination_without_cleanup(self):
        termination = FakeTermination(0)
        with (
            mock.patch("subprocess.Popen", side_effect=FileNotFoundError(2, "not found")),
            mock.patch("os.killpg") as kill,
            self.assertRaisesRegex(app_server.Error, "fake: not found"),
        ):
            app_server.AppServer.call(
                "fake", [], "hooks/list", env={}, wait=1, limit=1024, termination=termination
            )
        kill.assert_not_called()
        self.assertEqual(termination.checks.call_count, 2)

    def test_broken_pipe_reports_returncode_after_process_cleanup(self):
        proc = mock.Mock(pid=123, returncode=None)
        proc.stdout.fileno.return_value = 42
        proc.stdin.write.side_effect = BrokenPipeError

        def wait(**_kwargs):
            proc.returncode = 17
            return 17

        proc.wait.side_effect = wait
        with (
            mock.patch("subprocess.Popen", return_value=proc),
            mock.patch("select.select", return_value=([42], [], [])),
            mock.patch("os.read", return_value=b""),
            mock.patch("os.killpg") as kill,
            self.assertRaises(app_server.Exited) as caught,
        ):
            app_server.AppServer.call("fake", [], "hooks/list", env={}, wait=1, limit=1024)
        self.assertEqual(caught.exception.args, (17,))
        kill.assert_called_once_with(123, app_server.signal.SIGKILL)
        proc.stdin.close.assert_called_once_with()
        proc.stdout.close.assert_called_once_with()
        proc.wait.assert_any_call()

    def test_line_bounds_and_timeout(self):
        with (
            mock.patch("time.monotonic", return_value=1),
            self.assertRaisesRegex(app_server.Error, "no answer"),
        ):
            next(app_server.Lines(42, wait=0, limit=1))
        with (
            mock.patch("select.select", return_value=([42], [], [])),
            mock.patch("os.read", return_value=b"x"),
        ):
            lines = app_server.Lines(42, wait=999999999999, limit=8)
            lines.buf = bytearray(8)
            with self.assertRaisesRegex(app_server.Error, "over"):
                next(lines)

    def test_process_cleanup_kills_timed_out_group(self):
        proc = mock.Mock(pid=123)
        proc.stdout.fileno.return_value = 42
        proc.wait.side_effect = [app_server.subprocess.TimeoutExpired("fake", 1), None]
        with (
            mock.patch("subprocess.Popen", return_value=proc),
            mock.patch("select.select", side_effect=[([42], [], []), ([], [], [])]),
            mock.patch("os.read", return_value=b'{"id":1,"result":{}}\n{"id":2,"result":{}}\n'),
            mock.patch("os.killpg") as kill,
            mock.patch("time.monotonic", side_effect=[0, 0, 0, 0, 0, 6]),
        ):
            app_server.AppServer.call("fake", [], "hooks/list", env={}, wait=1, limit=1024)
        kill.assert_called_once_with(123, app_server.signal.SIGKILL)
        proc.stdout.close.assert_called_once()

    def test_normal_probe_exit_kills_remaining_owned_group(self):
        proc = mock.Mock(pid=123)
        proc.stdout.fileno.return_value = 42
        proc.wait.return_value = 0
        with (
            mock.patch("subprocess.Popen", return_value=proc),
            mock.patch("select.select", side_effect=[([42], [], []), ([], [], [])]),
            mock.patch("os.read", return_value=b'{"id":1,"result":{}}\n{"id":2,"result":{}}\n'),
            mock.patch("os.killpg") as kill,
        ):
            app_server.AppServer.call("fake", [], "hooks/list", env={}, wait=1, limit=1024)
        kill.assert_called_once_with(123, app_server.signal.SIGKILL)
        proc.wait.assert_called()


class ProbeTest(Case):
    ANSWER = (b'{"id":1,"result":{}}', b'{"id":2,"result":{"primary":1}}')

    @contextlib.contextmanager
    def server(self, *replies, error=None):
        proc = mock.Mock()
        proc.stdout.fileno.return_value = 42
        with (
            mock.patch("subprocess.Popen", side_effect=error, return_value=proc) as popen,
            mock.patch("select.select", return_value=([42], [], [])),
            mock.patch("os.read", side_effect=[b"".join(r + b"\n" for r in replies), b""]),
            mock.patch("os.killpg"),
        ):
            yield popen

    def test_rate_limits_come_back_as_the_answer(self):
        with self.server(*self.ANSWER) as popen:
            self.assertEqual(app_server.probe({"HOME": "/h"}), {"primary": 1})
        self.assertEqual(popen.call_args.args[0][0], "codex")
        self.assertEqual(popen.call_args.kwargs["env"], {"HOME": "/h"})

    def test_anything_but_an_answer_object_is_none(self):
        for name, replies, error in (
            ("list", (b'{"id":1,"result":{}}', b'{"id":2,"result":[]}'), None),
            ("error", (b'{"id":1,"result":{}}', b'{"id":2,"error":{"message":"no"}}'), None),
            ("exited", (), None),
            ("missing", (), FileNotFoundError(2, "No such file")),
        ):
            with self.subTest(name=name), self.server(*replies, error=error):
                self.assertIsNone(app_server.probe({}))

    def test_a_timeout_outside_zero_to_ten_seconds_runs_nothing(self):
        for timeout in (0, -1, 10.5, True, "5"):
            with self.subTest(timeout=timeout), self.server(*self.ANSWER) as popen:
                self.assertIsNone(app_server.probe({}, timeout))
                popen.assert_not_called()

    def test_main_prints_the_answer_or_null(self):
        nan = (b'{"id":1,"result":{}}', b'{"id":2,"result":{"primary":NaN}}')
        for argv, replies, printed in (
            ([], self.ANSWER, '{"primary": 1}'),
            (["--timeout", "2"], self.ANSWER, '{"primary": 1}'),
            (["--timeout", "x"], self.ANSWER, "null"),
            (["--timeout", "20"], self.ANSWER, "null"),
            (["5"], self.ANSWER, "null"),
            ([], nan, "null"),
        ):
            out = io.StringIO()
            with (
                self.subTest(argv=argv, replies=replies),
                self.server(*replies),
                mock.patch("sys.argv", ["app_server", *argv]),
                contextlib.redirect_stdout(out),
            ):
                app_server.main()
            self.assertEqual(out.getvalue(), printed + "\n")
