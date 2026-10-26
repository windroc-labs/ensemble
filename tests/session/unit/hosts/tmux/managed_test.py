"""Managed channel and conservative crash admission, without real processes."""

import os
import subprocess
import sys
import threading
import time
from unittest import mock

from ensemble.session.engine.types import (
    Key,
    KeyPress,
    Launch,
    Liveness,
    Paste,
    SessionBusy,
    Unanswered,
    WorkerExit,
    WorkerHook,
    WorkerOutput,
)
from ensemble.session.harness import guard
from ensemble.session.hosts.tmux import managed
from ensemble.session.hosts.tmux.host import TmuxHost
from tests.session.case import Case


class ManagedTest(Case):
    def test_live_lease_and_unclean_guard_death_refuse_second_owner(self):
        generation = "a" * 32
        directory = managed.location("tmux", "worker")
        managed.private_directory(directory + "/" + generation)
        managed.save_json(directory + "/current.json", {"generation": generation})
        managed.save_json(
            directory + "/" + generation + "/state.json",
            {"generation": generation, "state": "running"},
        )
        lease = managed.lease_file(directory + "/" + generation + "/lease")
        try:
            managed.fcntl.flock(lease, managed.fcntl.LOCK_EX)
            self.assertEqual(managed.liveness("tmux", "worker", generation), Liveness.LIVE)
        finally:
            os.close(lease)
        self.assertEqual(managed.liveness("tmux", "worker", generation), Liveness.UNKNOWN)
        with self.assertRaises(SessionBusy):
            TmuxHost("tmux", tmux=mock.Mock()).worker(
                "worker", Launch((sys.executable,), cwd=self.home), terminal=False
            )

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
            self.assertRaises(Unanswered),
        ):
            channel.write(b"x")
        channel = self.channel()
        with (
            mock.patch.object(time, "monotonic", side_effect=[0, 20]),
            self.assertRaises(Unanswered),
        ):
            channel.write(b"x")

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
        channel.proc.wait.side_effect = subprocess.TimeoutExpired("guard", 10)
        with self.assertRaises(Unanswered):
            channel.close()

    def test_bad_generation_and_missing_record_refuse_before_spawn(self):
        host = TmuxHost("tmux", tmux=mock.Mock())
        with self.assertRaises(managed.ConfigError):
            host.worker(
                "worker",
                Launch((sys.executable,), cwd=self.home),
                terminal=False,
                metadata={"generation": "../other"},
            )
        managed.private_directory(managed.location("tmux", "worker"))
        with mock.patch.object(subprocess, "Popen") as spawn, self.assertRaises(Unanswered):
            host.worker("worker", Launch((sys.executable,), cwd=self.home), terminal=False)
        spawn.assert_not_called()

    def test_worker_start_inherits_only_lease_into_guard(self):
        proc = mock.Mock()
        proc.stdout.read.side_effect = [
            guard.json_frame(b"s", {"pid": 123}),
            guard.json_frame(b"x", {"returncode": 0}),
            b"",
        ]
        host = TmuxHost("tmux", tmux=mock.Mock())
        with (
            mock.patch.object(subprocess, "Popen", return_value=proc) as spawn,
            mock.patch.object(os, "set_blocking"),
        ):
            channel = host.worker(
                "worker", Launch((sys.executable,), cwd=self.home), terminal=False
            )
            channel.close()
        options = spawn.call_args.kwargs
        self.assertTrue(options["close_fds"])
        self.assertEqual(len(options["pass_fds"]), 1)
        self.assertEqual(options["env"]["ENSEMBLE_GUARD_LEASE_FD"], str(options["pass_fds"][0]))
        self.assertIn("--hook-socket", spawn.call_args.args[0])
        self.assertIsNone(channel.record())
        self.assertIsNone(channel.display())

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

    def test_never_started_name_is_absent(self):
        self.assertEqual(managed.liveness("tmux", "worker", "a" * 32), Liveness.ABSENT)

    def test_existing_empty_directory_is_unknown(self):
        directory = managed.location("tmux", "worker")
        managed.private_directory(directory)
        self.assertEqual(managed.liveness("tmux", "worker", "a" * 32), Liveness.UNKNOWN)

    def test_symlink_directory_is_unknown(self):
        directory = managed.location("tmux", "worker")
        managed.private_directory(os.path.dirname(directory))
        os.symlink(self.home, directory)
        self.assertEqual(managed.liveness("tmux", "worker", "a" * 32), Liveness.UNKNOWN)

    def test_safe_json_rejects_links_and_permissions(self):
        path = self.home + "/state"
        managed.save_json(path, {"state": "exited"})
        self.assertEqual(managed.load_json(path), {"state": "exited"})
        os.chmod(path, 0o644)
        with self.assertRaises(OSError):
            managed.load_json(path)
        os.symlink(path, path + "-link")
        with self.assertRaises(OSError):
            managed.load_json(path + "-link")

    def test_ended_generation_requires_matching_record_and_free_lease(self):
        generation = "a" * 32
        directory = managed.location("tmux", "worker")
        managed.private_directory(directory + "/" + generation)
        managed.save_json(directory + "/current.json", {"generation": generation})
        managed.save_json(
            directory + "/" + generation + "/state.json",
            {"generation": generation, "state": "exited"},
        )
        with open(directory + "/" + generation + "/lease", "w"):
            pass
        os.chmod(directory + "/" + generation + "/lease", 0o600)
        self.assertEqual(managed.liveness("tmux", "worker", generation), Liveness.ABSENT)
        self.assertEqual(managed.liveness("tmux", "worker", "b" * 32), Liveness.UNKNOWN)

    def channel(self, *, terminal=False):
        return managed.ManagedProcess(
            "worker", "a" * 32, mock.Mock(), terminal=terminal, tmux=mock.Mock(), start_reader=False
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
