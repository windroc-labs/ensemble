"""Pure guard protocol and process-tree checks; no processes or credentials."""

import argparse
import errno
import json
import os
import signal
import subprocess
from unittest import mock

from ensemble.session.harness import guard
from tests.session.case import Case


class GuardTest(Case):
    def runtime(self, *, terminal=False):
        return guard.Guard(
            argparse.Namespace(
                hook_socket=self.home + "/hook",
                display_socket=None,
                terminal=terminal,
                command=["fake"],
            )
        )

    def test_fragmented_frames(self):
        decoder = guard.Decoder()
        wire = guard.frame(b"i", b"hello") + guard.frame(b"c", b"{}")
        got = []
        for byte in wire:
            got.extend(decoder.feed(bytes([byte])))
        self.assertEqual(got, [(b"i", b"hello"), (b"c", b"{}")])

    def test_frame_limit_before_payload(self):
        with self.assertRaises(ValueError):
            guard.Decoder().feed(b"i" + (guard.FRAME_MAX + 1).to_bytes(4, "big"))

    def test_hook_validation_and_stable_id(self):
        request = {"label": "done", "payload": {"sid": "original"}, "delivery_id": "a" * 32}
        self.assertEqual(guard.hook_message(json.dumps(request).encode()), request)
        for change in ({"delivery_id": "bad"}, {"payload": []}, {"label": ""}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                guard.hook_message(json.dumps({**request, **change}).encode())

    def test_tree_includes_detached_and_adopted_children(self):
        rows = {
            10: (1, "root", False),
            11: (10, "child", False),
            12: (11, "setsid", False),
            13: (99, "adopted", False),
            14: (1, "other", False),
        }
        self.assertEqual(
            guard.descendants(rows, {10: "root"}, 99),
            {10: "root", 11: "child", 12: "setsid", 13: "adopted"},
        )

    def test_pid_reuse_is_not_a_target(self):
        self.assertEqual(guard.descendants({10: (1, "new", False)}, {10: "old"}, 99), {})

    def test_escalation(self):
        self.assertEqual(
            [guard.close_signal(t) for t in (0, 0.49, 0.5, 1.49, 1.5, 9)],
            [
                signal.SIGHUP,
                signal.SIGHUP,
                signal.SIGTERM,
                signal.SIGTERM,
                signal.SIGKILL,
                signal.SIGKILL,
            ],
        )

    def test_screen_bound_and_escape_sequences(self):
        screen = guard.Screen(32)
        screen.append(b"old\n\x1b[31mhello\x1b[0m\r\nworld")
        self.assertEqual(screen.text(1), "world")
        self.assertNotIn("\x1b", screen.text(None))
        screen.append(b"x" * 100)
        self.assertLessEqual(len(screen.data), 32)

    def test_subreaper_failure_refuses_start(self):
        with (
            mock.patch.object(guard.sys, "platform", "linux"),
            mock.patch.object(guard.ctypes, "CDLL") as lib,
        ):
            lib.return_value.prctl.return_value = -1
            with self.assertRaises(OSError):
                guard.subreaper()

    def test_linux_stat_comm_can_contain_parentheses(self):
        fields = ["S", "8", "9", "9", *("0" for _ in range(15)), "123", "0"]
        self.assertEqual(guard.parse_stat("9 (a (b)) " + " ".join(fields)), (8, "123", False))

    def test_emit_partial_write_and_ack_only_after_whole_frame(self):
        runtime = self.runtime()
        peer = mock.Mock()
        runtime.peers[peer] = ("ack", bytearray())
        runtime.emit(b"hello", peer, "a" * 32)
        with mock.patch.object(os, "write", side_effect=[2, 3]):
            runtime.flush()
            peer.sendall.assert_not_called()
            runtime.flush()
        self.assertEqual(runtime.output_size, 0)
        self.assertEqual(
            json.loads(peer.sendall.call_args.args[0]), {"ok": True, "delivery_id": "a" * 32}
        )
        peer.close.assert_called_once()

    def test_blocked_stdout_does_not_block_close(self):
        runtime = self.runtime()
        runtime.emit(b"hello")
        with mock.patch.object(os, "write", side_effect=BlockingIOError):
            runtime.flush()
        runtime.close()
        first = runtime.closing
        runtime.close()
        self.assertEqual(runtime.closing, first)
        self.assertEqual(runtime.output_size, 5)

    def test_broken_stdout_starts_reclamation(self):
        runtime = self.runtime()
        runtime.emit(b"hello")
        with mock.patch.object(os, "write", side_effect=BrokenPipeError):
            runtime.flush()
        self.assertFalse(runtime.channel)
        self.assertIsNotNone(runtime.closing)
        self.assertFalse(runtime.output)

    def test_input_screen_close_and_invalid_commands(self):
        runtime = self.runtime()
        runtime.master = 8
        runtime.screen.append(b"hello")
        runtime.command(b"i", b"text")
        runtime.command(b"q", b'{"lines":1}')
        self.assertEqual(runtime.input, b"text")
        self.assertEqual(guard.Decoder().feed(runtime.output[0][0]), [(b"v", b'{"text":"hello"}')])
        for kind, payload in ((b"q", b'{"lines":false}'), (b"z", b""), (b"c", b"[]")):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                runtime.command(kind, payload)
        with mock.patch.object(guard, "BUFFER_MAX", 4), self.assertRaises(ValueError):
            runtime.command(b"i", b"overflow")
        runtime.command(b"c", b"{}")
        self.assertIsNotNone(runtime.closing)

    def test_hook_pending_ack_disconnect_and_invalid_shape(self):
        runtime = self.runtime()
        for data in (b"", b"bad\n", b"{}\n", b"a" * (guard.HOOK_MAX + 1)):
            peer = mock.Mock()
            peer.recv.return_value = data
            runtime.peers[peer] = ("hook", bytearray())
            runtime.peer_read(peer)
            self.assertNotIn(peer, runtime.peers)
        peer = mock.Mock()
        value = {"label": "done", "payload": {}, "delivery_id": "c" * 32}
        peer.recv.return_value = json.dumps(value).encode() + b"\n"
        runtime.peers[peer] = ("hook", bytearray())
        runtime.peer_read(peer)
        peer.sendall.assert_not_called()
        self.assertEqual(runtime.peers[peer][0], "ack")
        self.assertEqual(guard.Decoder().feed(runtime.output[0][0])[0][0], b"h")

    def test_display_input_and_backpressure(self):
        runtime = self.runtime()
        peer = mock.Mock()
        peer.recv.return_value = b"type"
        runtime.peers[peer] = ("display", bytearray())
        runtime.peer_read(peer)
        self.assertEqual(runtime.input, b"type")
        with mock.patch.object(guard, "BUFFER_MAX", 4):
            runtime.peer_read(peer)
        self.assertNotIn(peer, runtime.peers)

    def test_terminal_output_screen_and_slow_viewer(self):
        runtime = self.runtime()
        peer = mock.Mock()
        peer.send.return_value = 1
        runtime.peers[peer] = ("display", bytearray())
        runtime.streams[8] = b"t"
        with mock.patch.object(os, "read", return_value=b"hello"):
            runtime.read_stream(8)
        self.assertEqual(runtime.screen.text(None), "hello")
        self.assertNotIn(peer, runtime.peers)
        self.assertFalse(runtime.output)

    def test_headless_output_and_pty_eof(self):
        runtime = self.runtime()
        runtime.streams[8] = b"e"
        with mock.patch.object(os, "read", return_value=b"stderr"):
            runtime.read_stream(8)
        self.assertEqual(guard.Decoder().feed(runtime.output[0][0]), [(b"e", b"stderr")])
        with mock.patch.object(os, "read", side_effect=OSError(errno.EIO, "end")):
            runtime.read_stream(8)
        self.assertFalse(runtime.streams)

    def test_pidfd_prevents_recycled_pid_signal(self):
        with (
            mock.patch.object(os, "pidfd_open", return_value=9),
            mock.patch.object(os, "close") as closed,
            mock.patch.object(guard, "process_info", return_value=(1, "new", False)),
            mock.patch.object(signal, "pidfd_send_signal") as sent,
        ):
            guard.signal_process(8, "old", signal.SIGKILL)
        sent.assert_not_called()
        closed.assert_called_once_with(9)

    def test_tick_confirms_exit_after_adopted_children_reaped(self):
        runtime = self.runtime()
        runtime.proc = mock.Mock(returncode=0)
        runtime.known = {10: "client"}
        with (
            mock.patch.object(guard, "processes", return_value={}),
            mock.patch.object(os, "waitpid", side_effect=ChildProcessError),
        ):
            self.assertTrue(runtime.tick())

    def test_stopped_child_requires_escalation_and_no_false_confirmation(self):
        runtime = self.runtime()
        runtime.proc = mock.Mock(returncode=None)
        runtime.known = {10: "client"}
        runtime.closing = 1
        with (
            mock.patch.object(guard, "processes", return_value={10: (1, "client", False)}),
            mock.patch.object(guard.time, "monotonic", return_value=3),
            mock.patch.object(guard, "signal_process") as sent,
        ):
            self.assertFalse(runtime.tick())
            self.assertFalse(runtime.tick())
        sent.assert_called_once_with(10, "client", signal.SIGKILL)
        with (
            mock.patch.object(guard, "processes", return_value={10: (1, "client", False)}),
            mock.patch.object(guard.time, "monotonic", return_value=20),
            self.assertRaises(OSError),
        ):
            runtime.tick()

    def test_state_is_only_written_by_guard_after_confirmation(self):
        runtime = self.runtime()
        runtime.state("running")
        self.assertFalse(os.path.exists(self.home + "/state.json"))
        runtime.lease = "9"
        runtime.state("exited")
        with open(self.home + "/state.json") as stream:
            self.assertEqual(json.load(stream)["state"], "exited")

    def test_start_client_exec_failure_never_emits_started(self):
        runtime = self.runtime()
        with (
            mock.patch.object(guard, "subreaper"),
            mock.patch.object(guard, "listener"),
            mock.patch.object(subprocess, "Popen", side_effect=FileNotFoundError),
            self.assertRaises(FileNotFoundError),
        ):
            runtime.start()
        self.assertFalse(runtime.output)

    def test_run_places_exit_after_last_output(self):
        runtime = self.runtime()
        runtime.proc = mock.Mock(returncode=0, stdin=None, stdout=None, stderr=None)
        runtime.emit(guard.frame(b"o", b"last"))
        captured = []
        with (
            mock.patch.object(guard.Guard, "start"),
            mock.patch.object(guard.Guard, "tick", return_value=True),
            mock.patch.object(
                os, "write", side_effect=lambda _fd, data: captured.append(data) or len(data)
            ),
        ):
            self.assertEqual(runtime.run(), 0)
        self.assertEqual(
            [kind for kind, _ in guard.Decoder().feed(b"".join(captured))], [b"o", b"x"]
        )
