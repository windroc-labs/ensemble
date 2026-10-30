"""modes/headless.py over real Python children under a real local guard: the command's streams, its
state, its tree's end at stop, bounded waits, and a session running once started.
"""

import contextlib
import io
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from ensemble.session.engine.types import (
    Access,
    Launch,
    Reply,
    SessionError,
    Spec,
    Stderr,
    TurnFailed,
)
from ensemble.session.modes.base import POINTER
from ensemble.session.modes.headless import HeadlessMode
from tests.session.fakes.terminal import GuardHost
from tests.session.headless_support import NOTHING, SID, HeadlessCase, StubClient, StubRun
from tests.session.threads import promptly
from tests.utils.sandbox import kill

WAIT = 10  # upper bound on any wait, in seconds
SLEEPER = "import time; time.sleep(60)"
STUBBORN = (
    "import signal, time\n"
    "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "time.sleep(60)\n"
)
# Child code: starts a grandchild running SLEEPER on the child's pipes, prints its pid as {"text"};
# SPAWN_SLEEP then sleeps.
SPAWN = (
    "import json, subprocess, sys, time\n"
    "grandchild = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)'])\n"
    "print(json.dumps({'text': str(grandchild.pid)}), flush=True)\n"
)
SPAWN_SLEEP = SPAWN + "time.sleep(60)\n"
STREAMS = """import json, os, sys, time
print(json.dumps({"text": os.getcwd()}))
print("not json")
print(json.dumps({"diag": os.environ["HM_PROBE"]}))
sys.stdout.flush()
os.close(1)
time.sleep(0.3)
sys.stderr.buffer.write(b"bad \\xff\\n")
sys.exit(3)
"""
STDERR_FIRST = """import json, os, sys, time
sys.stderr.write("first\\n")
sys.stderr.flush()
os.close(2)
time.sleep(0.3)
print(json.dumps({"text": "late"}))
"""
ECHO = """import json, sys
print(json.dumps({"text": sys.argv[1]}), flush=True)
"""
RAISING = """import time
print('{"raise": "boom"}', flush=True)
time.sleep(60)
"""


class Base(HeadlessCase):
    def host(self) -> GuardHost:
        return GuardHost()

    def begin(self, argv: tuple[str, ...], env: dict | None = None, run=None):
        """self.mode started on argv in self.dir, env added to its child env; stopped at cleanup."""
        self.start(StubClient(Launch(argv, env=env or {}, cwd=self.dir), run))
        self.addCleanup(self.mode.stop)

    @contextlib.contextmanager
    def spied(self):
        """subprocess.Popen spied: each call's argv in the list."""
        calls, real = [], subprocess.Popen

        def popen(argv, **kwargs):
            calls.append(argv)
            return real(argv, **kwargs)

        with mock.patch("subprocess.Popen", side_effect=popen):
            yield calls

    def drain(self) -> list:
        """Every event up to both pipes' EOF."""
        found, deadline = [], time.monotonic() + WAIT
        while time.monotonic() < deadline:
            events, ended = self.mode.poll(0.1)
            found += events
            if ended:
                return found
        self.fail(f"not ended within {WAIT}s, got {found}")

    def first(self):
        """The next event, polled for up to WAIT s."""
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            events, _ = self.mode.poll(0.1)
            if events:
                return events[0]
        self.fail(f"no event within {WAIT}s")

    def grandchild(self, argv: tuple[str, ...]) -> None:
        """begin(argv) up to the grandchild's pid; the grandchild is killed at cleanup."""
        self.begin(argv)
        self.addCleanup(kill, int(self.first().text))

    def promptly(self, call):
        return promptly(self, call, WAIT)


class Promptly(Base):
    def test_preserves_original_errors_without_thread_stderr(self):
        for error in (RuntimeError("failed"), KeyboardInterrupt("interrupted")):
            with self.subTest(error=type(error).__name__):

                def fail(*, exception=error):
                    raise exception

                with (
                    mock.patch("sys.stderr", new=io.StringIO()) as console,
                    self.assertRaises(type(error)) as caught,
                ):
                    self.promptly(fail)
                self.assertIs(caught.exception, error)
                self.assertEqual(console.getvalue(), "")

    def test_returns_none_and_restores_the_thread_hook(self):
        hook = threading.excepthook
        self.assertIsNone(self.promptly(lambda: None))
        self.assertIs(threading.excepthook, hook)

    def test_delegates_other_threads_errors_to_the_previous_hook(self):
        error = RuntimeError("other thread")

        def fail():
            raise error

        def run_other():
            other = threading.Thread(target=fail)
            other.start()
            other.join(WAIT)
            self.assertFalse(other.is_alive())

        with mock.patch("threading.excepthook") as previous:
            self.assertIsNone(self.promptly(run_other))
            previous.assert_called_once()
            self.assertIs(previous.call_args.args[0].exc_value, error)
            self.assertIs(threading.excepthook, previous)


class Streams(Base):
    def test_stdout_and_stderr_lines_become_events_before_both_pipes_end(
        self,
    ):
        self.begin((sys.executable, "-I", "-c", STREAMS), env={"HM_PROBE": "probe"})
        found = self.drain()
        late = Stderr("bad �")
        self.assertEqual(
            [e for e in found if e != late],
            [
                Reply(os.path.realpath(self.dir)),
                Stderr("invalid JSON event"),
                Stderr("probe"),
                TurnFailed("invalid_output", "invalid JSON event"),
            ],
        )
        self.assertEqual(found.count(late), 1)
        self.assertEqual(self.promptly(lambda: self.mode.returncode(WAIT)), 3)
        self.assertEqual(self.mode.state(), "absent")
        self.mode.stop()
        self.assertEqual(self.mode.returncode(0), 3)

    def test_a_prompt_over_128_kib_starts_with_a_pointer_to_its_file(self):
        class Echo(StubRun):
            def launch(self, spec):
                super().launch(spec)
                return Launch((sys.executable, "-I", "-c", ECHO, spec.prompt))

        prompt, path = "雪 a\n" * 40_000, os.path.join(self.dir, "prompt", "prompt.md")
        self.start(StubClient(run=Echo), prompt=prompt)
        self.addCleanup(self.mode.stop)
        pointer = POINTER.format(size=len(prompt.encode()), path=path)
        self.assertEqual(self.drain(), [Reply(pointer), TurnFailed("incomplete_turn")])
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), prompt)

    def test_ended_only_after_stderr_then_stdout(self):
        self.begin((sys.executable, "-I", "-c", STDERR_FIRST))
        found = self.drain()
        self.assertCountEqual(
            found, [Stderr("first"), Reply("late"), TurnFailed("incomplete_turn", "first")]
        )
        self.assertEqual(self.promptly(lambda: self.mode.returncode(WAIT)), 0)

    def test_an_exception_from_parse_is_raised_from_poll(self):
        self.begin((sys.executable, "-I", "-c", RAISING))
        deadline = time.monotonic() + WAIT
        with self.assertRaisesRegex(RuntimeError, "boom"):
            while time.monotonic() < deadline:
                self.mode.poll(0.1)

    def test_parser_failure_stays_single_and_original_when_logging_is_disabled(self):
        error = KeyboardInterrupt("parser interrupted")

        class ErrorRun(StubRun):
            def parse(self, record):
                raise error

        self.addCleanup(logging.disable, logging.root.manager.disable)
        for level in (logging.NOTSET, logging.CRITICAL):
            with self.subTest(level=level), mock.patch("sys.stderr", new=io.StringIO()) as console:
                logging.disable(level)
                self.begin((sys.executable, "-I", "-c", "print('{}')"), run=ErrorRun)
                with self.assertRaises(KeyboardInterrupt) as caught:
                    self.drain()
                self.assertIs(caught.exception, error)
                self.mode.poll(0)
                self.mode.stop()
                self.assertEqual(console.getvalue(), "")


class Stop(Base):
    def test_kills_a_sleeping_child_whose_pipes_then_end(self):
        self.begin((sys.executable, "-I", "-c", SLEEPER))
        self.assertEqual(self.mode.state(), "live")
        self.promptly(self.mode.stop)
        self.assertEqual(self.mode.state(), "absent")
        self.assertLess(self.promptly(lambda: self.mode.returncode(WAIT)), 0)
        self.assertEqual(self.drain(), [TurnFailed("interrupted")])
        self.promptly(self.mode.stop)

    def test_ends_the_tree_so_a_grandchild_on_the_streams_ends_them(self):
        self.grandchild((sys.executable, "-I", "-c", SPAWN_SLEEP))
        self.promptly(self.mode.stop)
        self.assertLess(self.mode.returncode(0), 0)
        self.assertEqual(self.drain(), [TurnFailed("interrupted")])

    def test_a_grandchild_holding_the_streams_ends_with_the_client(self):
        self.grandchild((sys.executable, "-I", "-c", SPAWN))
        self.assertEqual(self.promptly(lambda: self.mode.returncode(WAIT)), 0)
        self.assertEqual(self.drain(), [TurnFailed("incomplete_turn")])

    def test_a_client_ignoring_hangup_and_terminate_is_still_ended(self):
        self.begin((sys.executable, "-I", "-c", STUBBORN))
        time.sleep(0.3)  # its handlers set
        self.promptly(self.mode.stop)
        self.assertEqual(self.mode.returncode(0), -signal.SIGKILL)
        self.assertEqual(self.mode.state(), "absent")

    def test_a_stop_after_its_natural_exit_changes_nothing(self):
        self.begin((sys.executable, "-I", "-c", "pass"))
        self.assertEqual(self.drain(), [TurnFailed("incomplete_turn")])
        self.assertEqual(self.promptly(lambda: self.mode.returncode(WAIT)), 0)
        self.promptly(self.mode.stop)
        self.assertEqual(self.mode.returncode(0), 0)

    def test_is_idempotent_once_started(self):
        self.begin((sys.executable, "-I", "-c", SLEEPER))
        self.promptly(self.mode.stop)
        self.promptly(self.mode.stop)
        self.assertLess(self.mode.returncode(0), 0)


class Start(Base):
    def test_a_missing_cwd_is_a_driver_error(self):
        with self.assertRaisesRegex(SessionError, r"\Aworker headless-[0-9a-f]{32}: cannot start "):
            self.start(StubClient(Launch(NOTHING, cwd=os.path.join(self.dir, "missing"))))
        self.assertEqual((len(self.recorded), self.mode), (1, None))

    def test_start_returns_its_session_running_at_once_with_its_prepared(self):
        argv = (sys.executable, "-I", "-c", SLEEPER)
        with self.spied() as calls:
            mode = HeadlessMode.start(
                Spec(workdir=self.dir, access=Access(cwd=self.dir), sid=SID, client="stub"),
                StubClient(Launch(argv)),
                registry=self.session_cls._config().registry,
                record=self.recorded.append,
                host=GuardHost(),
                generation="f" * 32,
            )
        self.addCleanup(mode.stop)
        self.assertIsInstance(mode, HeadlessMode)
        (guard,) = calls  # the guard; the client is its child
        self.assertEqual(
            (mode.prepared.info, mode.prepared.argv, tuple(guard[-len(argv) :])),
            (self.recorded[0], argv, argv),
        )
        self.assertEqual(
            (mode.state(), mode.poll(0), mode.returncode(0)), ("live", ([], False), None)
        )
        self.promptly(mode.stop)
        self.assertEqual(mode.state(), "absent")
        self.assertLess(mode.returncode(0), 0)


class Returncode(Base):
    def test_none_while_the_child_runs_after_waiting_at_most_timeout(self):
        self.begin((sys.executable, "-I", "-c", SLEEPER))
        began = time.monotonic()
        self.assertIsNone(self.promptly(lambda: self.mode.returncode(0.3)))
        self.assertGreaterEqual(time.monotonic() - began, 0.3)
        self.assertIsNone(self.promptly(lambda: self.mode.returncode(0)))


if __name__ == "__main__":
    unittest.main()
