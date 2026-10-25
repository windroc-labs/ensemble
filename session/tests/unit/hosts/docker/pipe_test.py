"""hosts/docker/pipe.py: DockerPiped's capped lines, side-effect-free wait, stop and its one close."""

import io
import os
import signal
import threading
from unittest import mock

from ensemble.session.engine.types import SessionError
from ensemble.session.hosts.docker.pipe import LINE_MAX, DockerPiped

from session.tests.case import Case
from utils.tests.process_fixtures import TimeoutExpired


class PipedTest(Case):
    def setUp(self):
        self.proc = mock.Mock(pid=4321, returncode=None, stdout=io.StringIO(), stderr=io.StringIO())
        self.closes = []
        read, self.lock = os.pipe()
        os.close(read)

    def piped(self, out="", err=""):
        self.proc.stdout, self.proc.stderr = io.StringIO(out), io.StringIO(err)
        return DockerPiped(
            "w",
            self.proc,
            close=lambda: self.closes.append(1),
            lock=self.lock,
            refresh=self.refresh,
        )

    def refresh(self, stopped):
        self.refreshing = stopped

    def test_lines_stream_back_and_their_reader_closes_the_pipe(self):
        piped = self.piped("a\nb", "e\n")
        with piped.stdout as out, piped.stderr as err:
            self.assertEqual((list(out), list(err)), (["a\n", "b"], ["e\n"]))
        self.assertTrue(self.proc.stdout.closed and self.proc.stderr.closed)

    def test_a_line_at_most_line_max_passes_and_a_longer_one_ends_the_stream(self):
        self.assertEqual(LINE_MAX, 8 << 20)
        full = "x" * LINE_MAX
        self.assertEqual(list(self.piped(full + "\n").stdout), [full + "\n"])
        lines = iter(self.piped(full + "x\nnext\n").stdout)
        with self.assertRaises(SessionError) as caught:
            next(lines)
        self.assertEqual(
            str(caught.exception), "docker session w: a client output line over 8388608 characters"
        )

    def test_wait_has_no_side_effect(self):
        piped = self.piped()
        self.proc.wait.side_effect = [TimeoutExpired("docker", 0), 0]
        self.assertEqual((piped.wait(0), piped.wait(1)), (None, 0))
        self.assertEqual(self.closes, [])
        os.fstat(self.lock)

    def test_close_runs_once_under_concurrent_calls_and_releases_the_lock(self):
        piped = self.piped()
        threads = [threading.Thread(target=piped.close) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        piped.close()
        self.assertEqual(self.closes, [1])
        with self.assertRaises(OSError):
            os.fstat(self.lock)

    def test_its_token_refresh_runs_until_close(self):
        piped = self.piped()
        self.assertTrue(self.refreshing.wait(0) is False)
        piped.close()
        self.assertTrue(self.refreshing.is_set())

    def test_stop_kills_the_exec_group_then_closes(self):
        piped = self.piped()
        with mock.patch("os.killpg") as killpg:
            piped.stop(ended=False)
        killpg.assert_called_once_with(4321, signal.SIGKILL)
        self.assertEqual(self.closes, [1])

    def test_a_failing_close_still_releases_the_lock(self):
        self.proc.stdout, self.proc.stderr = io.StringIO(), io.StringIO()
        piped = DockerPiped(
            "w",
            self.proc,
            close=mock.Mock(side_effect=OSError("x")),
            lock=self.lock,
            refresh=self.refresh,
        )
        with self.assertRaises(OSError):
            piped.close()
        with self.assertRaises(OSError):
            os.fstat(self.lock)
