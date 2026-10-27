"""hosts/docker/daemon.py: the capped run, over fake processes."""

import contextlib
import os
import threading
from unittest import mock

from ensemble.session.hosts.docker.daemon import OUT_MAX, Overflow, capped
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
