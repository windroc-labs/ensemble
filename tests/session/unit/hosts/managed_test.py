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
from tests.session.case import Case
from tests.utils.process_fixtures import TimeoutExpired


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
        channel.pump()
        self.assertEqual(
            channel.read(0),
            [
                WorkerHook("done", {"sid": "same"}, "a" * 32),
                WorkerOutput(b"diagnostic", "stderr"),
                WorkerExit(7),
            ],
        )
        self.assertEqual(channel.returncode(), 7)
        self.assertFalse(channel.alive())
        self.assertTrue(channel.wait_exit(0))

    def test_transport_eof_without_exit_is_unknown(self):
        channel = self.channel()
        channel.proc.stdout.read.side_effect = [guard.json_frame(b"s", {"pid": 123}), b""]
        channel.pump()
        channel.proc.wait.assert_called_once_with(timeout=managed.WAIT)
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
        with (
            mock.patch.object(
                managed.select, "select", return_value=([], [channel.proc.stdin], [])
            ),
            mock.patch.object(os, "write", side_effect=[BlockingIOError, 2, 4]) as written,
        ):
            channel.write(b"x")
        self.assertEqual(bytes(written.call_args.args[1]), b"\0\0\x01x")

    def test_write_failure_and_timeout_fail_closed(self):
        channel = self.channel()
        with (
            mock.patch.object(
                managed.select, "select", return_value=([], [channel.proc.stdin], [])
            ),
            mock.patch.object(os, "write", side_effect=BrokenPipeError),
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
            mock.patch.object(
                managed.select, "select", return_value=([], [channel.proc.stdin], [])
            ),
            mock.patch.object(os, "write", side_effect=BrokenPipeError),
            self.assertRaisesRegex(SessionError, "exited") as caught,
        ):
            channel.send(Paste("/color"))
        self.assertNotIsInstance(caught.exception, Unanswered)
        self.assertEqual((channel.state(), channel.returncode()), (Liveness.ABSENT, 0))
        self.assertEqual(channel.read(0), [WorkerExit(0)])

    def test_close_racing_the_clients_own_exit_succeeds(self):
        channel = self.channel(terminal=True)
        channel.accept(b"s", b'{"pid":123}')

        def gone(_fd, _data):
            channel.accept(b"x", b'{"returncode":0}')
            raise BrokenPipeError

        with (
            mock.patch.object(
                managed.select, "select", return_value=([], [channel.proc.stdin], [])
            ),
            mock.patch.object(os, "write", side_effect=gone),
        ):
            channel.close()
        self.assertEqual((channel.state(), channel.returncode()), (Liveness.ABSENT, 0))

    def test_terminal_input_is_encoded(self):
        channel = self.channel(terminal=True)
        written = []
        with (
            mock.patch.object(
                managed.select, "select", return_value=([], [channel.proc.stdin], [])
            ),
            mock.patch.object(
                os, "write", side_effect=lambda _fd, data: written.append(bytes(data)) or len(data)
            ),
        ):
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
        channel.proc.wait.assert_called_with(timeout=managed.WAIT)

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
        with (
            mock.patch.object(
                managed.select, "select", return_value=([], [channel.proc.stdin], [])
            ),
            mock.patch.object(os, "write", side_effect=write),
        ):
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

        with (
            mock.patch.object(
                managed.select, "select", return_value=([], [channel.proc.stdin], [])
            ),
            mock.patch.object(os, "write", side_effect=write),
        ):
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
            mock.patch.object(
                managed.select, "select", return_value=([], [channel.proc.stdin], [])
            ),
            mock.patch.object(os, "write", side_effect=write),
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

        with (
            mock.patch.object(
                managed.select, "select", return_value=([], [channel.proc.stdin], [])
            ),
            mock.patch.object(os, "write", side_effect=lambda _fd, data: len(data)) as written,
        ):
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
            mock.patch.object(
                managed.select, "select", return_value=([], [channel.proc.stdin], [])
            ),
            mock.patch.object(os, "write", side_effect=write) as written,
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
