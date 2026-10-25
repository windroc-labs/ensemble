"""Domain events encode through the existing one-line DelegateEvent contract; each is delivered to
the callback argv from the run dir in its own process group, holding the callback lock that names
its pid; a failed delivery is one callback_error row, a nonzero exit's naming its stderr. sweep kills the group a dead harness's
callback lock names, then waits for that lock.
"""

import contextlib
import fcntl
import io
import json
import os
import signal
from unittest import mock

from ensemble.delegate import ConfigError, DelegateEvent, Outcome, Progress
from ensemble.delegate.engine.callback import (
    CALLBACK_STOP_WAIT,
    CALLBACK_TIMEOUT,
    STDERR_TAIL,
    EventCallback,
    sweep,
)
from ensemble.delegate.engine.state import RunState
from ensemble.delegate.engine.types import Begun, Finished, Layout, Limit, Result
from ensemble.utils import OutcomeStatus, Stopped
from tests.delegate.case import Case
from tests.delegate.harness import SID
from tests.delegate.unit.engine.orphans import Clock, Orphan
from tests.utils.process_fixtures import DEVNULL, PIPE, TimeoutExpired

RUN = {"sid": SID, "attempt": 3}
ARGV = ("writeback", "--issue", "A B")
PID = 4242


class Codec(Case):
    def test_each_actual_domain_event_preserves_its_fields_in_one_line_stamped_with_its_run(self):
        outcome = Outcome(OutcomeStatus.DONE, "Title", "Summary", deliverable="# Body\n")
        events = (
            (Begun(SID), DelegateEvent("begun", **RUN)),
            (
                Progress("draft", "one\n二"),
                DelegateEvent("progress", **RUN, name="draft", text="one\n二"),
            ),
            (
                Finished(Result(7, outcome, "exit diagnostic")),
                DelegateEvent(
                    "finished",
                    **RUN,
                    returncode=7,
                    outcome=outcome,
                    error="exit diagnostic",
                ),
            ),
            (
                Finished(Result(0, None, "usage limit", Limit(1767225600.0))),
                DelegateEvent(
                    "finished",
                    **RUN,
                    returncode=0,
                    error="usage limit",
                    limit={"resets_at": 1767225600.0},
                ),
            ),
        )
        callback = EventCallback(("record",), RunState(Layout("/unused")), **RUN)
        for event, expected in events:
            with self.subTest(event=event):
                encoded = callback.encode(event)
                self.assertEqual(encoded, expected)
                line = encoded.to_line()
                self.assertEqual(line.splitlines(), [line])
                self.assertEqual(DelegateEvent.parse(line), expected)


class Child:
    """subprocess.Popen as EventCallback uses it, scripted by `Child.outcome`: an int is its exit
    code, an exception is raised by communicate; `Child.stderr` is what it wrote there. Records each
    spawn and call in `Child.calls`.
    """

    outcome: int | BaseException = 0
    stderr: str = ""
    calls: list = []

    def __init__(self, argv, **kwargs):
        Child.calls.append(("spawn", argv, kwargs))
        self.pid, self.returncode = PID, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        Child.calls.append(("exit",))

    def communicate(self, input=None, timeout=None):
        Child.calls.append(("communicate", input, timeout))
        if isinstance(Child.outcome, BaseException):
            raise Child.outcome
        self.returncode = Child.outcome
        return None, Child.stderr

    def wait(self, timeout=None):
        Child.calls.append(("wait",))
        self.returncode = -signal.SIGKILL
        return self.returncode

    def kill(self):
        Child.calls.append(("kill",))


def store(workdir: str) -> RunState:
    """A RunState whose run dir exists."""
    layout = Layout(workdir)
    os.makedirs(layout.run)
    return RunState(layout)


def close(descriptor: int) -> None:
    with contextlib.suppress(OSError):
        os.close(descriptor)


def failing_first_write(error: BaseException):
    """os.write raising `error` once: the pid write, the first write of a delivery."""
    write, raised = os.write, []

    def first(descriptor, data):
        if not raised:
            raised.append(error)
            raise error
        return write(descriptor, data)

    return mock.patch("os.write", side_effect=first)


class Delivery(Case):
    def setUp(self):
        self.store = store(os.path.join(self.home, "w"))
        self.work = self.store.layout.run
        Child.outcome, Child.stderr, Child.calls = 0, "", []
        self.popen = self.enterContext(mock.patch("subprocess.Popen", side_effect=Child))
        self.killpg = self.enterContext(
            mock.patch(
                "os.killpg",
                side_effect=lambda pid, signum: Child.calls.append(("killpg", pid, signum)),
            )
        )
        self.callback = EventCallback(ARGV, self.store, **RUN)

    def errors(self, work=None) -> list[dict]:
        try:
            with open(os.path.join(work or self.work, "run.jsonl"), encoding="utf-8") as file:
                rows = [json.loads(line) for line in file]
        except FileNotFoundError:
            return []
        return [row for row in rows if row["kind"] == "callback_error"]

    def lock_text(self) -> str:
        with open(self.store.callback_lock, encoding="utf-8") as file:
            return file.read()

    def test_one_line_on_stdin_of_the_argv_from_the_run_dir_in_its_own_group(self):
        event = Progress("draft", "one\n二")
        self.assertIsNone(self.callback(event))
        (spawn, communicate, _exit) = Child.calls
        _, argv, kwargs = spawn
        self.assertEqual(tuple(argv), ARGV)
        self.assertEqual(
            (
                kwargs["cwd"],
                kwargs["stdin"],
                kwargs["stdout"],
                kwargs["stderr"],
                kwargs["errors"],
                kwargs["process_group"],
            ),
            (self.work, PIPE, DEVNULL, PIPE, "replace", 0),
        )
        self.assertNotIn("shell", kwargs)
        _, given, timeout = communicate
        self.assertEqual(timeout, 300)
        self.assertEqual(given, self.callback.encode(event).to_line() + "\n")
        self.assertEqual(given.count("\n"), 1)
        self.assertEqual(DelegateEvent.parse(given).text, "one\n二")
        self.assertEqual(self.errors(), [])

    def test_a_failed_delivery_is_one_callback_error_row_without_a_retry(self):
        for index, (outcome, ending, error) in enumerate(
            (
                (7, ["communicate", "exit"], "callback exited 7"),
                (
                    TimeoutExpired(list(ARGV), CALLBACK_TIMEOUT),
                    ["communicate", "killpg", "wait", "exit"],
                    f"callback timed out after {CALLBACK_TIMEOUT} s",
                ),
            )
        ):
            with self.subTest(outcome=outcome):
                failed = store(os.path.join(self.home, f"failed-{index}"))
                work = failed.layout.run
                Child.outcome, Child.calls = outcome, []
                callback = EventCallback(ARGV, failed, **RUN)
                self.assertIsNone(callback(Begun(SID)))
                self.assertEqual([call[0] for call in Child.calls], ["spawn", *ending])
                if "killpg" in ending:
                    self.assertEqual(Child.calls[2], ("killpg", PID, signal.SIGKILL))
                self.assertEqual([row["error"] for row in self.errors(work)], [error])

    def test_a_nonzero_exit_names_its_last_stderr_line_and_keeps_its_tail(self):
        Child.outcome = 2
        Child.stderr = "warning: x\nensemble-orchestrator: config.local.toml: unknown key 'k'\n\n"
        with mock.patch("sys.stderr", new_callable=io.StringIO) as harness:
            self.assertIsNone(self.callback(Begun(SID)))
        (row,) = self.errors()
        self.assertEqual(
            row["error"],
            "callback exited 2: ensemble-orchestrator: config.local.toml: unknown key 'k'",
        )
        self.assertEqual(row["stderr"], Child.stderr)
        self.assertEqual(harness.getvalue(), Child.stderr)

    def test_a_long_stderr_keeps_only_its_tail(self):
        Child.outcome = 1
        Child.stderr = "a" * STDERR_TAIL + "b" * 10 + "\nlast"
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            self.callback(Begun(SID))
        (row,) = self.errors()
        self.assertEqual(row["stderr"], Child.stderr[-STDERR_TAIL:])
        self.assertEqual(len(row["stderr"]), STDERR_TAIL)
        self.assertEqual(row["error"], "callback exited 1: last")

    def test_a_successful_delivery_forwards_its_stderr_and_notes_nothing(self):
        Child.stderr = "note\n"
        with mock.patch("sys.stderr", new_callable=io.StringIO) as harness:
            self.callback(Begun(SID))
        self.assertEqual(harness.getvalue(), "note\n")
        self.assertEqual(self.errors(), [])

    def test_a_nonzero_exit_without_stderr_has_no_stderr_field(self):
        Child.outcome = 7
        self.callback(Begun(SID))
        (row,) = self.errors()
        self.assertEqual(row["error"], "callback exited 7")
        self.assertNotIn("stderr", row)

    def test_a_command_that_cannot_start_is_one_callback_error_row(self):
        self.popen.side_effect = FileNotFoundError(2, "No such file or directory", ARGV[0])
        self.assertIsNone(self.callback(Begun(SID)))
        self.assertEqual(self.popen.call_count, 1)
        (row,) = self.errors()
        self.assertIn("No such file or directory", row["error"])

    def test_an_exception_while_waiting_kills_and_reaps_the_group_then_propagates(self):
        stopped = Stopped(143)
        Child.outcome = stopped
        with self.assertRaises(Stopped) as raised:
            self.callback(Begun(SID))
        self.assertIs(raised.exception, stopped)
        self.assertEqual(
            [call[0] for call in Child.calls], ["spawn", "communicate", "killpg", "wait", "exit"]
        )
        self.assertEqual(Child.calls[2], ("killpg", PID, signal.SIGKILL))
        self.assertEqual(self.errors(), [])

    def test_the_child_inherits_the_held_callback_lock_naming_its_pid(self):
        seen = []

        def spawned(argv, **kwargs):
            seen.append((len(kwargs["pass_fds"]), self.store.held(self.store.callback_lock)))
            seen.append(self.lock_text())
            return Child(argv, **kwargs)

        self.popen.side_effect = spawned
        self.callback(Begun(SID))
        self.assertEqual(seen, [(1, True), ""])
        self.assertEqual(self.lock_text(), f"{PID}\n")
        self.assertFalse(self.store.held(self.store.callback_lock))

    def hold(self) -> int:
        holder = os.open(self.store.callback_lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX)
        return holder

    def test_a_callback_lock_held_5_s_spawns_nothing_and_is_one_callback_error_row(self):
        self.hold()
        clock = Clock(self)
        self.assertIsNone(self.callback(Begun(SID)))
        self.assertEqual(Child.calls, [])
        self.assertGreaterEqual(clock.now, CALLBACK_STOP_WAIT)
        self.assertEqual(
            [row["error"] for row in self.errors()],
            [f"callback lock {self.store.callback_lock} held: an earlier callback still runs"],
        )

    def test_a_callback_lock_freed_within_5_s_delivers(self):
        # a killed group's other members can hold the lock until they finish exiting
        holder = self.hold()
        self.enterContext(mock.patch("time.sleep", side_effect=lambda _seconds: os.close(holder)))
        self.assertIsNone(self.callback(Begun(SID)))
        self.assertEqual([call[0] for call in Child.calls], ["spawn", "communicate", "exit"])
        self.assertEqual(self.errors(), [])

    def test_a_callback_lock_that_is_no_regular_file_spawns_nothing_and_is_one_row(self):
        os.mkfifo(self.store.callback_lock)
        self.assertIsNone(self.callback(Begun(SID)))
        self.assertEqual(Child.calls, [])
        (error,) = [row["error"] for row in self.errors()]
        self.assertIn("not a regular lock file", error)

    def test_a_signal_while_naming_the_pid_kills_and_reaps_the_group_then_propagates(self):
        stopped = Stopped(143)
        with failing_first_write(stopped), self.assertRaises(Stopped) as raised:
            self.callback(Begun(SID))
        self.assertIs(raised.exception, stopped)
        self.assertEqual([call[0] for call in Child.calls], ["spawn", "killpg", "wait", "exit"])
        self.assertFalse(self.store.held(self.store.callback_lock))

    def test_a_failed_pid_write_kills_the_group_and_is_one_callback_error_row(self):
        failure = OSError(28, "No space left on device")
        with failing_first_write(failure):
            self.assertIsNone(self.callback(Begun(SID)))
        self.assertEqual([call[0] for call in Child.calls], ["spawn", "killpg", "wait", "exit"])
        self.assertEqual(
            [row["error"] for row in self.errors()],
            [f"callback lock {self.store.callback_lock}: [Errno 28] No space left on device"],
        )


class Sweep(Case):
    def setUp(self):
        self.store = store(os.path.join(self.home, "w"))
        self.clock = Clock(self)
        self.killpg = self.enterContext(mock.patch("os.killpg"))

    def orphan(self, text: str = f"{PID}\n") -> Orphan:
        orphan = Orphan(self, self.store.callback_lock, text)
        self.killpg.side_effect = orphan.killpg
        return orphan

    def held(self) -> bool:
        return self.store.held(self.store.callback_lock)

    def test_no_lock_or_a_free_one_sends_no_signal(self):
        sweep(self.store)
        with open(self.store.callback_lock, "w", encoding="utf-8") as file:
            file.write(f"{PID}\n")
        sweep(self.store)
        self.killpg.assert_not_called()
        self.assertEqual(self.clock.now, 0.0)

    def test_the_live_group_a_dead_harness_left_is_killed_then_its_lock_awaited(self):
        orphan = self.orphan()
        sweep(self.store)
        self.assertEqual(orphan.calls, [(PID, 0), (PID, signal.SIGKILL), (PID, 0)])
        self.assertFalse(self.held())

    def test_a_group_already_gone_gets_no_signal_and_its_lock_is_awaited(self):
        orphan = self.orphan()
        orphan.alive = False
        sweep(self.store)
        self.assertEqual(orphan.calls, [(PID, 0)])

    def test_a_group_alive_5_s_after_sigkill_is_a_config_error(self):
        orphan = self.orphan()
        orphan.dies = False
        with self.assertRaises(ConfigError) as caught:
            sweep(self.store)
        self.assertEqual(
            str(caught.exception),
            f"{self.store.callback_lock}: callback process group {PID} still exists "
            f"{CALLBACK_STOP_WAIT} s after SIGKILL",
        )
        self.assertEqual(orphan.calls[:2], [(PID, 0), (PID, signal.SIGKILL)])
        self.assertEqual(set(orphan.calls[2:]), {(PID, 0)})
        self.assertGreaterEqual(self.clock.now, CALLBACK_STOP_WAIT)

    def test_a_lock_still_held_5_s_after_its_group_ended_is_a_config_error(self):
        for alive, calls in (
            (True, [(PID, 0), (PID, signal.SIGKILL), (PID, 0)]),
            (False, [(PID, 0)]),
        ):
            with self.subTest(alive=alive):
                self.clock.now = 0.0
                orphan = self.orphan()
                orphan.alive, orphan.keeps = alive, True
                with self.assertRaises(ConfigError) as caught:
                    sweep(self.store)
                self.assertEqual(
                    str(caught.exception),
                    f"{self.store.callback_lock}: callback lock still held "
                    f"{CALLBACK_STOP_WAIT} s after its group ended",
                )
                self.assertEqual(orphan.calls, calls)
                orphan.free()

    def test_a_held_lock_without_a_pid_above_1_sends_no_signal(self):
        for text in ("", "x", "1", "0", "-3", " 12a", "12 ", "١٢", "9" * 5000):
            with self.subTest(text=text[:20]):
                orphan = self.orphan(text)
                with self.assertRaises(ConfigError) as caught:
                    sweep(self.store)
                self.assertEqual(
                    str(caught.exception),
                    f"{self.store.callback_lock}: invalid callback pid {text!r}",
                )
                self.killpg.assert_not_called()
                orphan.free()

    def test_a_group_it_may_not_signal_or_no_pid_at_all_is_a_config_error(self):
        huge, denied = 2**40, PermissionError(1, "Operation not permitted")
        for text, signum, error, calls in (
            (f"{PID}\n", 0, denied, [(PID, 0)]),
            (f"{PID}\n", signal.SIGKILL, denied, [(PID, 0), (PID, signal.SIGKILL)]),
            (f"{huge}\n", 0, OverflowError("signed integer is greater than maximum"), [(huge, 0)]),
        ):
            with self.subTest(signum=signum, error=error):
                orphan = self.orphan(text)
                orphan.errors[signum] = error
                pid = int(text)
                with self.assertRaises(ConfigError) as caught:
                    sweep(self.store)
                self.assertEqual(
                    str(caught.exception),
                    f"{self.store.callback_lock}: cannot signal callback process group {pid}: "
                    f"{error}",
                )
                self.assertEqual(orphan.calls, calls)
                orphan.free()

    def test_a_group_ending_between_its_probe_and_its_sigkill_gets_its_lock_awaited(self):
        orphan = self.orphan()

        def ended(pid, signum):
            if signum == signal.SIGKILL:
                orphan.alive = False
                orphan.calls.append((pid, signum))
                orphan.free()
                raise ProcessLookupError(3, "No such process")
            orphan.killpg(pid, signum)

        self.killpg.side_effect = ended
        sweep(self.store)
        self.assertEqual(orphan.calls, [(PID, 0), (PID, signal.SIGKILL), (PID, 0)])
        self.assertFalse(self.held())

    def test_a_run_lock_that_is_no_regular_file_is_a_config_error(self):
        os.mkdir(os.path.join(self.store.layout.run, "delegate.run.lock"))
        with self.assertRaises(ConfigError) as caught:
            sweep(self.store)
        self.assertEqual(
            str(caught.exception), f"{self.store.layout.workdir}: invalid delegate run lock"
        )

    def test_a_held_run_lock_is_a_live_run_whose_callback_it_never_signals(self):
        self.orphan()
        with self.store.run_lock(), self.assertRaises(ConfigError) as caught:
            sweep(self.store)
        self.assertEqual(
            str(caught.exception), f"{self.store.layout.workdir}: delegate is still alive"
        )
        self.killpg.assert_not_called()
