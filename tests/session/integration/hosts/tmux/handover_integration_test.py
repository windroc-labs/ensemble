"""hosts/tmux/host.py's launch handoff child processes, no tmux: the pane wrapper (EXEC) as tmux runs it and
the file standing alone.
"""

import errno
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from contextlib import suppress

from ensemble.session.engine import tmux_cli
from tests.session.case import Case
from tests.utils.sandbox import scratch

WAIT = """import os, sys, time
for _ in range(3000):
    if os.path.exists(sys.argv[1]): break
    time.sleep(0.01)"""


class ExecTest(Case):
    def setUp(self):
        self.root = scratch(self)
        self.path, self.fifo = (os.path.join(self.root, n) for n in ("handover.json", "errpipe"))
        os.mkfifo(self.fifo, 0o600)
        self.reader = os.open(self.fifo, os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, self.reader)

    def run_exec(self, argv, env, cwd, pane):
        """The wrapper as tmux runs it, given a handover file naming argv and the errpipe."""
        with open(self.path, "w") as f:
            json.dump({"argv": argv, "env": env, "cwd": cwd}, f)
        return subprocess.run(
            self.wrapper(),
            env=pane,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
        )

    def wrapper(self):
        """The shipped wrapper argv, its errpipe self.fifo."""
        return [*tmux_cli._wrapper(self.root)[1][:-2], self.path, self.fifo]

    def piped(self):
        """What the wrapper wrote to the errpipe, read to EOF; BlockingIOError while a writer holds
        it open.
        """
        out = b""
        while chunk := os.read(self.reader, 64):
            out += chunk
        return out

    def test_runs_the_handover(self):
        cwd = os.path.join(self.root, "cwd")
        os.makedirs(cwd)
        code = "import json, os, sys; print(json.dumps([os.getcwd(), dict(os.environ), sys.argv]))"
        env = {"A": "1", "B": "two words"}
        pane = {"TERM": "tmux-256color", "PANE_ONLY": "1", "PATH": os.environ.get("PATH", "")}
        res = self.run_exec([sys.executable, "-c", code, "x", "y z"], env, cwd, pane)
        self.assertEqual(res.returncode, 0, res.stderr)
        got_cwd, got_env, got_argv = json.loads(res.stdout)
        self.assertEqual(got_cwd, cwd)
        self.assertEqual(
            {k: got_env.get(k) for k in (*env, "TERM")}, {**env, "TERM": "tmux-256color"}
        )
        self.assertNotIn("PANE_ONLY", got_env)
        self.assertNotIn("PATH", got_env)
        self.assertEqual(got_argv, ["-c", "x", "y z"])
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(self.piped(), b"")

    def test_execve_closes_the_errpipe_while_argv_runs(self):
        """EOF with no data once argv runs: the wrapper's end never reaches argv."""
        go = os.path.join(self.root, "go")
        with open(self.path, "w") as f:
            json.dump({"argv": [sys.executable, "-c", WAIT, go], "env": {}, "cwd": self.root}, f)
        wrapper = subprocess.Popen(
            self.wrapper(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
        )
        self.addCleanup(lambda: wrapper.returncode is None and wrapper.kill())
        piped, deadline = None, time.monotonic() + 10
        while piped is None and time.monotonic() < deadline:
            time.sleep(0.01)
            if not os.path.exists(self.path):  # a FIFO no writer opened yet reads as EOF too
                with suppress(BlockingIOError):
                    piped = self.piped()
        self.assertEqual(piped, b"")
        self.assertIsNone(wrapper.poll())
        open(go, "w").close()
        out, err = wrapper.communicate(timeout=30)
        self.assertEqual((wrapper.returncode, out), (0, ""), err)

    def test_an_exec_failure_writes_its_errno_and_argv_never_runs(self):
        for text, number in (
            ("#!/nonexistent/interpreter\nprint('ran')\n", errno.ENOENT),
            ("echo ran\n", errno.ENOEXEC),
        ):
            with self.subTest(text=text):
                program = os.path.join(self.root, "program")
                with open(program, "w") as f:
                    f.write(text)
                os.chmod(program, 0o755)
                res = self.run_exec([program], {}, self.root, {})
                self.assertEqual((res.returncode, res.stdout), (1, ""))
                self.assertEqual(self.piped(), b"%d" % number)
                self.assertFalse(os.path.exists(self.path))

    def test_any_other_exec_failure_writes_einval_and_argv_never_runs(self):
        for env in ({"X": "a\0b"}, {"X": "\ud800"}, {"X=Y": "1"}):
            with self.subTest(env=env):
                res = self.run_exec([sys.executable, "-c", "print('ran')"], env, self.root, {})
                self.assertEqual((res.returncode, res.stdout), (1, ""))
                self.assertIn("Error: ", res.stderr)
                self.assertEqual(self.piped(), b"%d" % errno.EINVAL)

    def test_a_cwd_it_cannot_enter_fails_it_leaving_the_handover_untaken(self):
        res = self.run_exec(
            [sys.executable, "-c", "print('ran')"], {}, os.path.join(self.root, "missing"), {}
        )
        self.assertEqual((res.returncode, res.stdout), (1, ""))
        self.assertIn("FileNotFoundError", res.stderr)
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(self.piped(), b"%d" % errno.ENOENT)

    def test_an_errpipe_without_a_reader_runs_nothing_leaving_the_handover_untaken(self):
        """The caller gone: ENXIO."""
        self.fifo = os.path.join(self.root, "unread")
        os.mkfifo(self.fifo, 0o600)
        res = self.run_exec([sys.executable, "-c", "print('ran')"], {}, self.root, {})
        self.assertEqual((res.returncode, res.stdout), (1, ""))
        self.assertIn(f"[Errno {errno.ENXIO}]", res.stderr)
        self.assertTrue(os.path.exists(self.path))

    def test_default_signal_dispositions(self):
        for sig in (signal.SIGPIPE, signal.SIGXFSZ):
            with self.subTest(sig=sig.name):
                res = self.run_exec(
                    ["/bin/sh", "-c", f"kill -{sig.name[3:]} $$"], {}, self.root, {}
                )
                self.assertEqual(res.returncode, -sig, res.stderr)


if __name__ == "__main__":
    unittest.main()
