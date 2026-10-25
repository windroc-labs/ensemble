"""The opencode launcher in process over a fake child: arguments, output, exit codes, the watch."""

import io
import json
import os
import signal
import threading
from unittest import mock

from ensemble.session.clients.opencode import launcher

from session.tests.case import Case

BOUND = json.dumps({"type": "step_start", "sessionID": "ses_a"}).encode() + b"\n"
TEXT = b'{"type": "text"}\n'


class Sink(io.BytesIO):
    def close(self):
        self.written = self.getvalue()
        super().close()


class Child:
    def __init__(self, lines=(), code=0, err=b""):
        self.stdin, self.stdout, self.code, self.killed = Sink(), lines, code, False
        read, write = os.pipe()
        os.write(write, err)
        os.close(write)
        self.stderr = os.fdopen(read, "rb")

    def wait(self):
        return self.code

    def kill(self):
        self.killed = True


class Timer:
    def __init__(self, seconds, function):
        self.seconds, self.function, self.cancelled = seconds, function, False
        self.daemon = False

    def start(self):
        Timers.made.append(self)

    def cancel(self):
        self.cancelled = True


class Timers:
    made: list[Timer] = []


class LauncherTest(Case):
    def setUp(self):
        Timers.made = []
        self.out, self.err = io.TextIOWrapper(io.BytesIO()), io.TextIOWrapper(io.BytesIO())
        for target, value in (
            ("sys.stdout", self.out),
            ("sys.stderr", self.err),
            ("ensemble.session.clients.opencode.launcher.threading.Timer", Timer),
        ):
            self.enterContext(mock.patch(target, value))

    def run_with(self, child, *, prompt="Hi there") -> int:
        self.addCleanup(child.stderr.close)
        argv = ["--timeout", "120", "--prompt", prompt, "--", "opencode", "run"]
        with mock.patch.object(launcher.subprocess, "Popen", return_value=child) as popen:
            code = launcher.main(argv)
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], ["opencode", "run"])
        return code

    def written(self) -> tuple[bytes, str]:
        self.out.flush()
        self.err.flush()
        return self.out.buffer.getvalue(), self.err.buffer.getvalue().decode()

    def test_output_prompt_and_exit_code_pass_through(self):
        child = Child([BOUND, TEXT], code=3, err=b"oops\n")
        self.assertEqual(self.run_with(child), 3)
        self.assertEqual(self.written(), (BOUND + TEXT, "oops\n"))
        self.assertEqual(child.stdin.written, b"Hi there")
        (timer,) = Timers.made
        self.assertEqual(timer.seconds, 120.0)
        self.assertTrue(timer.cancelled)

    def test_a_signal_is_128_plus_its_number(self):
        self.assertEqual(self.run_with(Child([BOUND], code=-signal.SIGKILL)), 128 + signal.SIGKILL)

    def test_the_watch_kills_a_run_without_a_session(self):
        child = Child()

        def lines():
            yield TEXT
            Timers.made[0].function()
            yield BOUND

        child.stdout = lines()
        with mock.patch("os._exit", side_effect=SystemExit) as end, self.assertRaises(SystemExit):
            self.run_with(child)
        end.assert_called_once_with(124)
        self.assertTrue(child.killed)
        self.assertEqual(self.written(), (TEXT, "opencode: no session after 120 s; stopped\n"))

    def test_the_watch_spares_a_bound_run(self):
        child = Child()

        def lines():
            yield BOUND
            Timers.made[0].function()
            yield TEXT

        child.stdout = lines()
        self.assertEqual(self.run_with(child), 0)
        self.assertFalse(child.killed)
        self.assertEqual(self.written()[0], BOUND + TEXT)

    def test_the_watch_stops_a_run_whose_reader_hangs(self):
        class Stuck(io.BytesIO):
            def write(self, data):
                Timers.made[0].function()  # the watch fires while this write holds its lock
                return super().write(data)

        stuck = mock.Mock(buffer=Stuck())
        child = Child([TEXT])
        with (
            mock.patch("sys.stdout", stuck),
            mock.patch("os._exit", side_effect=SystemExit) as end,
            self.assertRaises(SystemExit),
        ):
            self.run_with(child)
        end.assert_called_once_with(124)
        self.assertTrue(child.killed)

    def test_the_watch_spares_a_run_bound_while_its_lock_wait_times_out(self):
        waits, bound = [], threading.Event()
        expiry = threading.Thread(target=lambda: Timers.made[0].function())

        class Gate:
            """The watch's lock, noting each timed wait and keeping it short."""

            def __init__(self, lock):
                self.lock, self.waiting = lock, threading.Event()

            def __enter__(self):
                return self.lock.__enter__()

            def __exit__(self, *exc):
                return self.lock.__exit__(*exc)

            def acquire(self, **kwargs):
                self.waiting.set()
                bound.wait(5)  # the line binds before this wait times out
                waits.append((kwargs, self.lock.acquire(timeout=0.01)))
                return waits[-1][1]

            def release(self):
                self.lock.release()

        loads = json.loads

        def binding(line):
            # The watch fires unbound and waits on the lock this line holds; the line binds meanwhile.
            if expiry.ident is None:
                watch = Timers.made[0].function.__self__
                watch.lock = Gate(watch.lock)
                expiry.start()
                self.assertTrue(watch.lock.waiting.wait(5))
            return loads(line)

        class Stuck(io.BytesIO):
            def write(self, data):
                bound.set()
                expiry.join(5)  # holds the lock until the wait times out
                return super().write(data)

        stdout, child = Stuck(), Child([BOUND, TEXT])
        with (
            mock.patch("json.loads", binding),
            mock.patch("sys.stdout", mock.Mock(buffer=stdout)),
            mock.patch("os._exit", side_effect=SystemExit) as end,
        ):
            self.assertEqual(self.run_with(child), 0)
        self.assertEqual(waits, [({"timeout": launcher.LOCK_WAIT}, False)])
        end.assert_not_called()
        self.assertFalse(child.killed)
        self.assertEqual((stdout.getvalue(), self.written()[1]), (BOUND + TEXT, ""))

    def test_a_bound_or_ended_run_skips_the_lock(self):
        for state in ("session", "ended"):
            with self.subTest(state=state):
                child = Child()
                self.addCleanup(child.stderr.close)
                watch = launcher.Watch(child, "120")
                setattr(watch, state, True)
                watch.lock = mock.Mock()
                watch.expire()
                watch.lock.acquire.assert_not_called()
                self.assertFalse(child.killed)

    def test_a_run_bound_or_ended_during_the_lock_wait_is_spared(self):
        for state in ("session", "ended"):
            with self.subTest(state=state):
                child = Child()
                self.addCleanup(child.stderr.close)
                watch = launcher.Watch(child, "120")

                def change(watch=watch, state=state, **_):
                    setattr(watch, state, True)
                    return False

                watch.lock = mock.Mock(**{"acquire.side_effect": change})
                with mock.patch("os._exit", side_effect=SystemExit) as end:
                    watch.expire()
                end.assert_not_called()
                watch.lock.release.assert_not_called()
                self.assertFalse(child.killed)

    def test_the_watch_spares_a_run_that_ended(self):
        child = Child([TEXT], code=1)
        self.assertEqual(self.run_with(child), 1)
        Timers.made[0].function()
        self.assertFalse(child.killed)

    def test_a_closed_stdout_kills_the_child(self):
        def lines():
            yield BOUND
            raise OSError("broken pipe")

        child = Child(code=-signal.SIGKILL)
        child.stdout = lines()
        self.assertEqual(self.run_with(child), 128 + signal.SIGKILL)
        self.assertTrue(child.killed)

    def test_a_command_it_cannot_start(self):
        argv = ["--timeout", "1", "--prompt", "", "--", "opencode"]
        for error, message in (
            (FileNotFoundError(2, "No such file"), "opencode: command not found: opencode\n"),
            (
                PermissionError(13, "Permission denied"),
                "opencode: cannot run opencode: Permission denied\n",
            ),
        ):
            with self.subTest(error=error):
                self.err.buffer.seek(0)
                self.err.buffer.truncate()
                with mock.patch.object(launcher.subprocess, "Popen", side_effect=error):
                    self.assertEqual(launcher.main(argv), 127)
                self.assertEqual(self.written()[1], message)

    def test_bad_arguments(self):
        for argv in (
            [],
            ["--timeout", "x", "--prompt", "", "--", "a"],
            ["--timeout", "0", "--prompt", "", "--", "a"],
            ["--timeout", "inf", "--prompt", "", "--", "a"],
            ["--time", "1", "--prompt", "", "--", "a"],
            ["--timeout", "1", "--prompt", ""],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(launcher.main(argv), 2)
        self.assertTrue(self.written()[1].startswith("usage: "))

    def test_bound_needs_a_json_object_with_a_string_session(self):
        for line, bound in (
            (BOUND, True),
            (b'{"sessionID": 1}', False),
            (b"[]", False),
            (b"not json", False),
        ):
            with self.subTest(line=line):
                self.assertIs(launcher._bound(line), bound)
