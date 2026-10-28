"""The shared guard channel host end, without real processes."""

import json
import os
import threading
import time
from unittest import mock

from ensemble.session.engine.types import (
    Key,
    KeyPress,
    Liveness,
    Paste,
    SessionError,
    Unanswered,
    WorkerExit,
    WorkerHook,
    WorkerOutput,
)
from ensemble.session.harness import guard
from ensemble.session.hosts import managed
from ensemble.session.hosts.base import GUARD_WAIT
from tests.session.case import Case
from tests.session.unit.hosts.support import writable
from tests.utils.process_fixtures import TimeoutExpired

WAIT = 5  # seconds for a background thread's step


class ManagedProcessTest(Case):
    def test_modified_control_keys_do_not_become_invalid_csi_sequences(self):
        for key, expected in (
            (KeyPress("Enter", meta=True), b"\x1b\r"),
            (KeyPress("Escape", meta=True), b"\x1b\x1b"),
            (KeyPress("Tab", meta=True), b"\x1b\t"),
            (KeyPress("Tab", shift=True), b"\x1b[Z"),
            (KeyPress("BSpace", meta=True), b"\x1b\x7f"),
            (KeyPress("BSpace", ctrl=True), b"\x08"),
            (KeyPress("Enter", ctrl=True), b"\r"),
            (KeyPress(" ", ctrl=True), b"\x00"),
            (KeyPress("/", ctrl=True), b"\x1f"),
            (KeyPress("?", ctrl=True), b"\x7f"),
        ):
            with self.subTest(key=key):
                self.assertEqual(managed.key_bytes(key), expected)

    def test_reader_drains_after_overflow_instead_of_waiting_for_consumer(self):
        channel = self.channel()
        wire = guard.json_frame(b"s", {"pid": 1}) + guard.frame(
            b"o", b"x" * (managed.QUEUE_MAX + 1)
        )
        channel.proc.stdout.read.side_effect = [wire, b"more", b"last", b""]
        channel.pump()
        self.assertEqual(channel.proc.stdout.read.call_count, 4)
        with self.assertRaises(Unanswered):
            channel.read(0)

    def test_pump_fragmentation_hooks_and_final_exit(self):
        channel = self.channel()
        value = {"label": "done", "payload": {"sid": "same"}, "delivery_id": "a" * 32}
        wire = (
            guard.json_frame(b"s", {"pid": 123})
            + guard.json_frame(b"h", value)
            + guard.frame(b"e", b"diagnostic")
            + guard.json_frame(b"x", {"returncode": 7})
        )
        channel.proc.stdout.read.side_effect = [wire[:3], wire[3:], b""]
        with writable():
            channel.pump()
        self.assertEqual(
            channel.read(0),
            [
                WorkerHook("done", {"sid": "same"}, "a" * 32),
                WorkerOutput(b"diagnostic", "stderr"),
                WorkerExit(7),
            ],
        )
        self.assertEqual((channel.returncode(), channel.state()), (7, Liveness.ABSENT))

    def test_each_hook_is_released_to_its_guard_only_after_it_was_taken(self):
        channel = self.channel()
        order, released = [], threading.Event()
        release = guard.frame(b"k", b"{}")
        taken = channel.accept_hook
        channel.accept_hook = lambda data: order.append("taken") or taken(data)
        value = {"label": "Stop", "payload": {}, "delivery_id": "a" * 32}
        reads = iter(
            [
                guard.json_frame(b"s", {"pid": 1})
                + guard.json_frame(b"h", value)
                + guard.json_frame(b"h", {**value, "delivery_id": "b" * 32})
            ]
        )

        def read(_size):  # EOF only once both were released
            for data in reads:
                return data
            released.wait(WAIT)
            return b""

        def write(_fd, wire):
            order.append(bytes(wire))
            if order.count(release) == 2:
                released.set()
            return len(wire)

        channel.proc.stdout.read.side_effect = read
        with writable(write):
            channel.pump()
        self.assertTrue(released.is_set())
        releases = [i for i, item in enumerate(order) if item == release]
        takes = [i for i, item in enumerate(order) if item == "taken"]
        self.assertEqual(len(takes), 2)
        self.assertTrue(all(r > t for r, t in zip(releases, takes, strict=True)))

    def test_a_writer_waiting_on_a_gone_guard_never_holds_the_reader(self):
        """A final hook, then a write meeting the gone guard: the reader still takes its exit."""
        channel = self.channel()
        entered = threading.Event()
        closed = channel.input_closed

        def input_closed(error):  # its send lock held, waiting for this reader's exit frame
            entered.set()
            return closed(error)

        channel.input_closed = input_closed
        value = {"label": "Stop", "payload": {}, "delivery_id": "a" * 32}
        reads = iter(
            [
                guard.json_frame(b"s", {"pid": 1}),
                lambda: (
                    entered.wait(WAIT)
                    and guard.json_frame(b"h", value) + guard.json_frame(b"x", {"returncode": 0})
                ),
            ]
        )

        def read(_size):
            for data in reads:
                return data() if callable(data) else data
            return b""

        errors = []

        def writer():
            try:
                channel.write(b"late")
            except SessionError as error:
                errors.append(error)

        channel.proc.stdout.read.side_effect = read
        thread = threading.Thread(target=writer)
        with writable(BrokenPipeError):
            thread.start()
            began = time.monotonic()
            channel.pump()
            thread.join(WAIT)
        self.assertLess(time.monotonic() - began, GUARD_WAIT / 2)
        self.assertIsNone(channel.failure)
        self.assertEqual(channel.returncode(), 0)
        self.assertEqual([type(error) for error in errors], [SessionError])

    def test_a_release_still_writing_at_close_keeps_a_clean_exit(self):
        """A k already counted before the exit frame: close never shuts stdin under its write."""
        channel = self.channel()
        channel.accept(b"s", b'{"pid":1}')
        writing, closed = threading.Event(), threading.Event()
        channel.proc.stdin.close.side_effect = closed.set

        def write(_fd, _wire):  # the gone guard's pipe, unless its stdin was shut first
            writing.set()
            if closed.wait(WAIT / 10):
                raise ValueError("I/O operation on closed file")
            raise BrokenPipeError

        with writable(write):
            channel.release_hook()
            self.assertTrue(writing.wait(WAIT))
            channel.accept(b"x", b'{"returncode":0}')
            channel.close()
            assert channel.releaser is not None
            channel.releaser.join(WAIT)
        self.assertIsNone(channel.failure)
        self.assertEqual((channel.state(), channel.returncode()), (Liveness.ABSENT, 0))

    def test_an_idle_releaser_ends_once_its_stdin_is_closing(self):
        """No exit yet (an unanswered close): ending releases never waits out GUARD_WAIT."""
        channel = self.channel()
        channel.accept(b"s", b'{"pid":1}')
        with writable():
            channel.release_hook()
            began = time.monotonic()
            channel.end_releases()
        self.assertLess(time.monotonic() - began, GUARD_WAIT / 2)
        assert channel.releaser is not None
        self.assertFalse(channel.releaser.is_alive())
        self.assertIsNone(channel.failure)

    def test_transport_eof_without_exit_is_unknown(self):
        channel = self.channel()
        channel.proc.stdout.read.side_effect = [guard.json_frame(b"s", {"pid": 123}), b""]
        channel.pump()
        channel.proc.wait.assert_called_once_with(timeout=GUARD_WAIT)
        self.assertEqual(channel.state(), Liveness.UNKNOWN)
        with self.assertRaises(Unanswered):
            channel.close()

    def test_reader_rejects_malformed_protocol_and_continues_drain(self):
        for wire in (guard.frame(b"z", b"{}"), b"o" + (guard.FRAME_MAX + 1).to_bytes(4, "big")):
            channel = self.channel()
            channel.proc.stdout.read.side_effect = [wire, b"more", b""]
            channel.pump()
            self.assertEqual(channel.state(), Liveness.UNKNOWN)
            self.assertEqual(channel.proc.stdout.read.call_count, 3)

    def test_write_retries_backpressure_and_does_not_spawn(self):
        channel = self.channel()
        with writable([BlockingIOError, 2, 4]) as written:
            channel.write(b"x")
        self.assertEqual(bytes(written.call_args.args[1]), b"\0\0\x01x")

    def test_write_failure_and_timeout_fail_closed(self):
        channel = self.channel()
        with (
            writable(BrokenPipeError),
            mock.patch.object(threading.Condition, "wait_for", return_value=False),
            self.assertRaises(Unanswered),
        ):
            channel.write(b"x")
        channel = self.channel()
        with (
            mock.patch.object(time, "monotonic", side_effect=[0, 20]),
            self.assertRaises(Unanswered),
        ):
            channel.write(b"x")

    def test_input_after_a_confirmed_exit_is_refused_without_failing(self):
        """A guard that confirmed its client's exit and went: input fails, the exit stands."""
        channel = self.channel(terminal=True)
        channel.accept(b"s", b'{"pid":123}')
        channel.accept(b"x", b'{"returncode":0}')
        with (
            writable(BrokenPipeError),
            self.assertRaisesRegex(SessionError, "exited") as caught,
        ):
            channel.send(Paste("/color"))
        self.assertNotIsInstance(caught.exception, Unanswered)
        self.assertEqual((channel.state(), channel.returncode()), (Liveness.ABSENT, 0))
        self.assertEqual(channel.read(0), [WorkerExit(0)])

    def test_an_empty_write_ends_headless_input_and_later_writes_are_refused(self):
        channel = self.channel()
        channel.accept(b"s", b'{"pid":123}')
        written = []
        with writable(lambda _fd, data: written.append(bytes(data)) or len(data)):
            channel.write(b"queued")
            channel.write(b"")
            for data in (b"late", b""):
                with self.assertRaisesRegex(SessionError, "^worker worker: input ended$"):
                    channel.write(data)
        self.assertEqual(guard.Decoder().feed(b"".join(written)), [(b"i", b"queued"), (b"i", b"")])
        self.assertEqual(channel.state(), Liveness.LIVE)

    def test_a_write_after_the_exit_frame_is_refused_while_the_guard_lingers(self):
        channel = self.channel()
        channel.accept(b"s", b'{"pid":123}')
        channel.accept(b"x", b'{"returncode":0}')
        with mock.patch.object(os, "write") as write:
            for data in (b"x", b""):
                with self.assertRaisesRegex(SessionError, "^worker worker: exited$") as caught:
                    channel.write(data)
                self.assertNotIsInstance(caught.exception, Unanswered)
        write.assert_not_called()
        self.assertEqual(channel.read(0), [WorkerExit(0)])

    def test_an_empty_write_on_a_terminal_handle_sends_nothing(self):
        channel = self.channel(terminal=True)
        channel.accept(b"s", b'{"pid":123}')
        written = []
        with writable(lambda _fd, data: written.append(bytes(data)) or len(data)):
            channel.write(b"")
            channel.press([])
            channel.write(b"typed")
        self.assertEqual(guard.Decoder().feed(b"".join(written)), [(b"i", b"typed")])

    def test_close_racing_the_clients_own_exit_succeeds(self):
        channel = self.channel(terminal=True)
        channel.accept(b"s", b'{"pid":123}')

        def gone(_fd, _data):
            channel.accept(b"x", b'{"returncode":0}')
            raise BrokenPipeError

        with writable(gone):
            channel.close()
        self.assertEqual((channel.state(), channel.returncode()), (Liveness.ABSENT, 0))

    def test_terminal_input_is_encoded(self):
        channel = self.channel(terminal=True)
        written = []
        with writable(lambda _fd, data: written.append(bytes(data)) or len(data)):
            channel.send(Paste("hello"))
            channel.send(Key("Enter"))
            channel.press([KeyPress("c", ctrl=True)])
        self.assertEqual(
            [data for _, data in guard.Decoder().feed(b"".join(written))],
            [b"\x1b[200~hello\x1b[201~", b"\r", b"\x03"],
        )
        for key, expected in (
            (KeyPress("Up", ctrl=True), b"\x1b[1;5A"),
            (KeyPress("PageUp", shift=True), b"\x1b[5;2~"),
            (KeyPress("x", meta=True), b"\x1bx"),
            (KeyPress("a", shift=True), b"A"),
        ):
            self.assertEqual(managed.key_bytes(key), expected)

    def test_close_is_idempotent_and_keeps_output_for_reader(self):
        channel = self.channel()
        channel.accept(b"s", b'{"pid":123}')
        channel.accept(b"o", b"last")
        channel.accept(b"x", b'{"returncode":0}')
        channel.close()
        channel.close()
        self.assertEqual(channel.read(0), [WorkerOutput(b"last"), WorkerExit(0)])
        channel.proc.wait.assert_called_with(timeout=GUARD_WAIT)

    def test_confirmed_worker_but_live_guard_does_not_report_close_success(self):
        channel = self.channel()
        channel.accept(b"s", b'{"pid":123}')
        channel.accept(b"x", b'{"returncode":0}')
        channel.proc.wait.side_effect = TimeoutExpired("guard", 10)
        with self.assertRaises(Unanswered):
            channel.close()

    def test_start_confirmation_timeout_closes_pipe(self):
        channel = self.channel()
        with (
            mock.patch.object(threading.Condition, "wait_for", return_value=False),
            self.assertRaises(Unanswered),
        ):
            channel.await_start()
        channel.proc.stdin.close.assert_called_once()

    def test_screen_validation_and_reply(self):
        channel = self.channel(terminal=True)
        with self.assertRaises(managed.ConfigError):
            channel.screen(lines=False)

        def write(_fd, data):
            channel.accept(b"v", b'{"text":"current screen"}')
            return len(data)

        channel.accept(b"s", b'{"pid":123}')
        with writable(write):
            self.assertEqual(channel.screen(3), "current screen")

    def test_screen_timeout_drains_delayed_reply_before_next_query(self):
        channel = self.channel(terminal=True)
        channel.accept(b"s", b'{"pid":123}')
        requests = []
        replies = iter(("old one-line screen", "new forty-line screen"))
        requests_at_reply = []

        def write(_fd, data):
            requests.extend(json.loads(payload) for _, payload in guard.Decoder().feed(data))
            return len(data)

        def wait(predicate, _timeout):
            if not predicate():
                requests_at_reply.append(list(requests))
                channel.accept(b"v", json.dumps({"text": next(replies)}).encode())
            return predicate()

        with writable(write):
            with (
                mock.patch.object(
                    threading.Condition, "wait_for", side_effect=lambda predicate, _: predicate()
                ),
                self.assertRaisesRegex(Unanswered, "screen unanswered"),
            ):
                channel.screen(lines=1)
            with mock.patch.object(threading.Condition, "wait_for", side_effect=wait):
                self.assertEqual(channel.screen(lines=40), "new forty-line screen")
        self.assertEqual(requests, [{"lines": 1}, {"lines": 40}])
        self.assertEqual(requests_at_reply, [[{"lines": 1}], [{"lines": 1}, {"lines": 40}]])
        self.assertEqual(channel.state(), Liveness.LIVE)
        channel.proc.stdin.close.assert_not_called()

    def test_screen_repeated_timeouts_keep_one_outstanding_query(self):
        channel = self.channel(terminal=True)
        channel.accept(b"s", b'{"pid":123}')
        requests = []

        def write(_fd, data):
            requests.extend(json.loads(payload) for _, payload in guard.Decoder().feed(data))
            return len(data)

        with (
            writable(write),
            mock.patch.object(
                threading.Condition, "wait_for", side_effect=lambda predicate, _: predicate()
            ),
        ):
            for lines in (1, 40, 80):
                with self.assertRaisesRegex(Unanswered, "screen unanswered"):
                    channel.screen(lines=lines)
            self.assertEqual(requests, [{"lines": 1}])
            channel.accept(b"v", b'{"text":"late"}')

            def reply(predicate, _timeout):
                if not predicate():
                    channel.accept(b"v", b'{"text":""}')
                return predicate()

            with mock.patch.object(threading.Condition, "wait_for", side_effect=reply):
                self.assertEqual(channel.screen(lines=2), "")
        self.assertEqual(requests, [{"lines": 1}, {"lines": 2}])

    def test_screen_failure_while_draining_does_not_send_another_query(self):
        channel = self.channel(terminal=True)
        channel.accept(b"s", b'{"pid":123}')

        def fail(predicate, _timeout):
            if not predicate():
                channel.fail("broken transport")
            return predicate()

        with writable() as written:
            with (
                mock.patch.object(
                    threading.Condition, "wait_for", side_effect=lambda predicate, _: predicate()
                ),
                self.assertRaisesRegex(Unanswered, "screen unanswered"),
            ):
                channel.screen(lines=1)
            with (
                mock.patch.object(threading.Condition, "wait_for", side_effect=fail),
                self.assertRaisesRegex(Unanswered, "broken transport"),
            ):
                channel.screen(lines=40)
        self.assertEqual(written.call_count, 1)
        self.assertEqual(channel.state(), Liveness.UNKNOWN)
        channel.proc.stdin.close.assert_called_once()

    def test_screen_encoding_failure_does_not_leave_an_outstanding_query(self):
        channel = self.channel(terminal=True)
        channel.accept(b"s", b'{"pid":123}')

        def write(_fd, data):
            channel.accept(b"v", b'{"text":"current"}')
            return len(data)

        with (
            writable(write) as written,
            mock.patch.object(
                threading.Condition, "wait_for", side_effect=lambda predicate, _: predicate()
            ),
        ):
            with (
                mock.patch.object(json, "dumps", side_effect=ValueError("cannot encode query")),
                self.assertRaisesRegex(ValueError, "cannot encode query"),
            ):
                channel.screen(lines=1)
            written.assert_not_called()
            self.assertEqual(channel.screen(lines=40), "current")
        self.assertEqual(written.call_count, 1)
        self.assertEqual(channel.state(), Liveness.LIVE)
        channel.proc.stdin.close.assert_not_called()

    def channel(self, *, terminal=False):
        return managed.ManagedProcess(
            "worker", "a" * 32, mock.Mock(), terminal=terminal, start_reader=False
        )

    def test_output_then_exit_once(self):
        channel = self.channel()
        channel.accept(b"s", b'{"pid":123}')
        channel.accept(b"o", b"last reply")
        channel.accept(b"x", b'{"returncode":0}')
        self.assertEqual(channel.read(0), [WorkerOutput(b"last reply"), WorkerExit(0)])
        self.assertEqual(channel.read(0), [])
        self.assertEqual(channel.returncode(), 0)
        with self.assertRaises(ValueError):
            channel.accept(b"o", b"late")

    def test_up_marks_its_client_up_and_queues_nothing(self):
        channel = self.channel()
        channel.accept(b"s", b'{"pid":123}')
        self.assertFalse(channel.up)
        channel.accept(b"u", b"{}")
        self.assertTrue(channel.up)
        self.assertEqual(channel.read(0), [])
        channel.accept(b"x", b'{"returncode":0}')
        self.assertTrue(channel.up)

    def test_an_up_before_start_twice_after_exit_or_with_a_payload_fails(self):
        for frames in (
            [(b"u", b"{}")],
            [(b"s", b'{"pid":123}'), (b"u", b"{}"), (b"u", b"{}")],
            [(b"s", b'{"pid":123}'), (b"x", b'{"returncode":0}'), (b"u", b"{}")],
            [(b"s", b'{"pid":123}'), (b"u", b'{"after":5}')],
        ):
            with self.subTest(frames=frames), self.assertRaises(ValueError):
                channel = self.channel()
                for kind, payload in frames:
                    channel.accept(kind, payload)

    def test_overflow_fails_and_closes_lifecycle(self):
        channel = self.channel()
        channel.accept(b"s", b'{"pid":123}')
        channel.accept(b"o", b"x" * (managed.QUEUE_MAX + 1))
        with self.assertRaises(Unanswered):
            channel.read(0)
        channel.proc.stdin.close.assert_called_once()
        self.assertEqual(channel.state(), Liveness.UNKNOWN)

    def test_headless_rejects_terminal_operations(self):
        channel = self.channel()
        for operation in (lambda: channel.screen(), lambda: channel.press(())):
            with self.assertRaises(managed.NotShowable):
                operation()

    def test_invalid_frames_and_exit_shape(self):
        for kind, payload in (
            (b"s", b'{"pid":false}'),
            (b"x", b'{"returncode":false}'),
            (b"z", b"{}"),
        ):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.channel().accept(kind, payload)

    def test_deeply_nested_frames_fail_closed_and_keep_draining(self):
        nested = b'{"label":"done","payload":' + b"[" * 100000 + b"]" * 100000 + b"}"
        for kind in (b"h", b"x"):
            with self.subTest(kind=kind):
                channel = self.channel()
                channel.proc.stdout.read.side_effect = [
                    guard.json_frame(b"s", {"pid": 1}) + guard.frame(kind, nested),
                    b"more",
                    b"",
                ]
                channel.pump()
                self.assertEqual(channel.state(), Liveness.UNKNOWN)
                self.assertEqual(channel.proc.stdout.read.call_count, 3)

    def test_dropped_hook_queues_nothing(self):
        class Dropping(managed.ManagedProcess):
            def accept_hook(self, data):
                return None

        channel = Dropping("worker", "a" * 32, mock.Mock(), terminal=False, start_reader=False)
        channel.accept(b"s", b'{"pid":1}')
        channel.accept(
            b"h", json.dumps({"label": "done", "payload": {}, "delivery_id": "a" * 32}).encode()
        )
        self.assertEqual(channel.read(0), [])

    def test_on_exit_runs_once_after_exit_or_failure(self):
        calls = []

        class Told(managed.ManagedProcess):
            def on_exit(self):
                calls.append(self.code)

        for end in ("exit", "failure"):
            with self.subTest(end=end):
                calls.clear()
                channel = Told("worker", "a" * 32, mock.Mock(), terminal=False, start_reader=False)
                channel.accept(b"s", b'{"pid":1}')
                if end == "exit":
                    channel.accept(b"x", b'{"returncode":3}')
                channel.fail("broken")
                channel.fail("again")
                assert channel.exit_thread is not None
                channel.exit_thread.join(5)
                self.assertEqual(calls, [3] if end == "exit" else [None])
