"""tests/session/fakes/host.py: FakeHost's refusals, before any process starts (the rest: its
integration test).
"""

import itertools
import json
import os
import signal
import sys
import unittest
from pathlib import Path
from unittest import mock

from ensemble.session import ConfigError, Launch, SessionError
from ensemble.utils import ProcessControl
from tests.session.case import Case
from tests.session.fakes.host import SLEEP, FakeHost, FakeProcess


class FakeHostOnly(Case):
    def test_refuses_a_bad_name_argv_or_cwd(self):
        host = FakeHost("fake", {"root": "~/hosts"})
        good = (list(SLEEP), self.home, {})
        for name, argv, cwd in (
            ("a b", *good[:2]),
            ("../w", *good[:2]),
            ("", *good[:2]),
            ("w1", [], self.home),
            ("w1", good[0], os.path.join(self.home, "absent")),
        ):
            for kind in ("run", "run_terminal"):
                with (
                    self.subTest(kind=kind, name=name, argv=argv, cwd=cwd),
                    self.assertRaises(ValueError),
                ):
                    getattr(host, kind)(name, Launch(tuple(argv), cwd=cwd))
        self.assertEqual(host.list(), [])

    def test_launch_rejects_malformed_argv_before_the_host_can_start(self):
        host = FakeHost("fake", {"root": "~/hosts"})
        for argv in ("python3", (sys.executable, 1), ("tool", "a\0b")):
            for kind in ("run", "run_terminal"):
                with (
                    self.subTest(kind=kind, argv=argv),
                    self.assertRaisesRegex(
                        ConfigError, "^launch: argv must be strings without NUL$"
                    ),
                ):
                    getattr(host, kind)("w1", Launch(argv, cwd=self.home))
        self.assertEqual(host.list(), [])


class FailedStart(Case):
    """A start that fails after its control was prepared leaves no controller or supervisor."""

    def start(self, supervisor, *, pid_file):
        def popen(argv, **_kw):
            if pid_file:
                Path(argv[4], "pid").write_text(str(supervisor.pid))
            return supervisor

        host = FakeHost("fake", {"root": "~/hosts"})
        with (
            mock.patch.dict(os.environ, {"ENSEMBLE_CONTROL_ROOT": self.home}),
            mock.patch("subprocess.Popen", side_effect=popen),
            mock.patch("os.killpg") as killpg,
            mock.patch("time.monotonic", side_effect=itertools.count(0, 100)),
            mock.patch("time.sleep"),
            self.assertRaises((SessionError, OSError)) as raised,
        ):
            host.run("w1", Launch(SLEEP, cwd=self.home))
        meta = json.loads(Path(self.home, "hosts", "w1", "meta.json").read_text())
        self.assertFalse(os.path.exists(meta["control"]))
        return raised.exception, killpg

    def test_an_exited_supervisor_is_not_signalled(self):
        error, killpg = self.start(mock.Mock(pid=4242, returncode=1), pid_file=False)
        self.assertIsInstance(error, SessionError)
        self.assertRegex(str(error), "did not start")
        killpg.assert_not_called()

    def test_a_running_supervisor_is_killed_when_the_start_is_not_confirmed(self):
        error, killpg = self.start(mock.Mock(pid=4242, returncode=None), pid_file=True)
        self.assertIsInstance(error, TimeoutError)
        killpg.assert_called_once_with(4242, signal.SIGKILL)


class ControlTest(Case):
    def process(self):
        folder = Path(self.home, "session")
        folder.mkdir()
        control = ProcessControl(str(Path(self.home, "controller")))
        Path(control.path).mkdir(mode=0o700)
        self.addCleanup(control.remove)
        Path(folder, "meta.json").write_text(json.dumps({"id": "first", "control": control.path}))
        return FakeProcess("mine", str(folder)), control

    def test_completed_generation_does_not_signal_after_unobserved_exit_or_replacement(self):
        process, control = self.process()
        Path(control.path, "state.json").write_text('{"done": 0}')
        Path(process.folder, "meta.json").write_text('{"id": "replacement"}')
        with mock.patch("os.killpg") as killpg, mock.patch("socket.socket") as socket:
            process.kill()
            process.close()
            self.assertTrue(process.wait_exit(0))
            self.assertEqual(
                json.loads(Path(process.folder, "meta.json").read_text())["id"], "replacement"
            )
            killpg.assert_not_called()
            socket.assert_not_called()

    def test_permission_denial_is_never_a_successful_exit_or_lost_identity(self):
        process, control = self.process()
        with mock.patch("socket.socket") as socket:
            connection = socket.return_value.__enter__.return_value
            stream = connection.makefile.return_value.__enter__.return_value
            stream.readline.side_effect = [
                b'{"error": "permission denied", "errno": 1}\n',
                b'{"error": "permission denied", "errno": 1}\n',
                b'{"live": false}\n',
            ]
            with self.assertRaisesRegex(SessionError, "permission denied"):
                process.wait_exit(0)
            with self.assertRaisesRegex(SessionError, "permission denied"):
                process.kill()
            process.kill()
            self.assertTrue(process.wait_exit(0))
            self.assertEqual(
                connection.connect.call_args_list,
                [mock.call(os.path.join(control.path, "control"))] * 3,
            )


if __name__ == "__main__":
    unittest.main()
