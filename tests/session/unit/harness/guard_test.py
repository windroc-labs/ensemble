"""Pure guard protocol and process-tree checks; no processes or credentials."""

import argparse
import contextlib
import errno
import io
import json
import os
import signal
import struct
import sys
import termios
import tty
from collections import deque
from pathlib import Path
from unittest import mock

from ensemble.session.harness import guard
from ensemble.session.hosts.base import GUARD_HOOK_SOCKET
from tests.session.case import Case


@contextlib.contextmanager
def spawned(*, terminal=True, sockets=None, proc=None):
    """A guard's start with no real spawn, socket or (terminal) pty; yields (ioctl, Popen, socket)."""
    with contextlib.ExitStack() as stack:

        def patch(target, name, **options):
            return stack.enter_context(mock.patch.object(target, name, **options))

        patch(guard.ctypes, "CDLL").return_value.prctl.return_value = 0
        socket = patch(guard.socket, "socket", side_effect=sockets)
        ioctl = patch(guard.fcntl, "ioctl") if terminal else None
        if terminal:
            patch(os, "openpty", return_value=(20, 21))
            patch(os, "close")
        patch(os, "chmod")
        patch(os, "set_blocking")
        patch(os, "listdir", return_value=[])
        yield (
            ioctl,
            patch(guard.subprocess, "Popen", return_value=proc or mock.Mock(pid=100)),
            socket,
        )


class Base(Case):
    def runtime(self, *, terminal=False, display_grace=False, size=guard.SIZE, **options):
        args = argparse.Namespace(
            hook_socket=self.home + "/hook",
            display_socket=None,
            display_grace=display_grace,
            terminal=terminal,
            size=size,
            command=["fake"],
        )
        return guard.Guard(args, **options)


class StartupDisplayTest(Base):
    def output(self, runtime, data):
        for start in range(0, len(data), guard.READ_SIZE):
            with mock.patch.object(os, "read", return_value=data[start : start + guard.READ_SIZE]):
                runtime.read_stream(8)

    def terminal(self, data):
        runtime = self.runtime(terminal=True)
        runtime.screen = mock.Mock()
        runtime.screen.snapshot.return_value = b"current screen"
        runtime.streams[8] = b"t"
        self.addCleanup(self.cleanup, runtime)
        self.output(runtime, data)
        return runtime

    @staticmethod
    def cleanup(runtime):
        startup = getattr(runtime, "startup", None)
        if startup is not None:
            startup.stream.close()

    @staticmethod
    def connect(runtime, send):
        server, peer = mock.Mock(), mock.Mock()
        server.accept.return_value = (peer, "")
        peer.send.side_effect = send
        runtime.take_display(server)
        return peer

    def test_complete_colored_startup_exceeding_memory_limit_is_replayed(self):
        unit = "\x1b[31m启动\x1b[0m\r\n".encode()
        data = unit * (guard.BUFFER_MAX // len(unit) + 1)
        runtime = self.terminal(data)
        sent = []
        peer = self.connect(runtime, lambda chunk: sent.append(bytes(chunk)) or len(chunk))
        for _ in range(len(data) // guard.READ_SIZE + 4):
            runtime.write_display(peer)
        self.assertEqual(b"".join(sent), data)
        self.assertIsNone(runtime.startup)

    def test_short_writes_and_eagain_keep_startup_before_live_bytes(self):
        startup = "\x1b[31m早\x1b[0m\r\n".encode()
        live = b"\x1b[32mlive\x1b[0m\r\n"
        runtime = self.terminal(startup)
        sent, attempts = [], []

        def send(chunk):
            attempts.append(None)
            if len(attempts) == 1:
                raise BlockingIOError
            sent.append(bytes(chunk[:2]))
            return min(2, len(chunk))

        peer = self.connect(runtime, send)
        self.assertIn(peer, runtime.peers)
        self.output(runtime, live)
        for _ in range(len(startup) + len(live)):
            runtime.write_display(peer)
        self.assertEqual(b"".join(sent), startup + live)
        self.assertIsNone(runtime.startup)

    def test_slow_first_display_releases_spool_without_closing_worker(self):
        runtime = self.terminal(b"startup")
        stream = runtime.startup.stream
        peer = self.connect(runtime, mock.Mock(side_effect=BlockingIOError))
        self.output(runtime, b"x" * (guard.BUFFER_MAX + 1))
        self.assertNotIn(peer, runtime.peers)
        self.assertTrue(stream.closed)
        self.assertIsNone(runtime.startup)
        self.assertIsNone(runtime.closing)
        self.assertFalse(runtime.output)

    def test_disconnect_releases_startup_and_next_display_gets_current_screen(self):
        runtime = self.terminal(b"startup")
        stream = runtime.startup.stream
        peer = self.connect(runtime, mock.Mock(side_effect=BlockingIOError))
        runtime.drop(peer)
        self.assertTrue(stream.closed)
        sent = []
        self.connect(runtime, lambda chunk: sent.append(bytes(chunk)) or len(chunk))
        self.assertEqual(b"".join(sent), b"current screen")

    def test_display_send_failure_closes_only_that_display_and_the_spool(self):
        for result in (BrokenPipeError(), 0):
            with self.subTest(result=result):
                runtime = self.terminal(b"startup")
                stream = runtime.startup.stream
                send = mock.Mock()
                if isinstance(result, BaseException):
                    send.side_effect = result
                else:
                    send.return_value = result
                peer = self.connect(runtime, send)
                self.assertNotIn(peer, runtime.peers)
                self.assertTrue(stream.closed)
                self.assertIsNone(runtime.closing)

    def test_exit_grace_drains_the_replay_before_returning(self):
        data = b"\x1b[31m" + b"x" * (guard.READ_SIZE + 17)
        runtime = self.runtime(terminal=True, display_grace=True)
        self.addCleanup(self.cleanup, runtime)
        runtime.screen = mock.Mock()
        runtime.screen.snapshot.return_value = b"current screen"
        runtime.args.display_socket = self.home + "/display"
        hook_server, display_server, peer = mock.Mock(), mock.Mock(), mock.Mock()
        hook_server.accept.side_effect = BlockingIOError
        display_server.accept.side_effect = [(peer, ""), *([BlockingIOError] * 100)]
        proc = mock.Mock(pid=100, returncode=None)
        chunks = iter([data[: guard.READ_SIZE], data[guard.READ_SIZE :], b""])
        now, sent = [100.0], []

        def read(_fd, _size):
            chunk = next(chunks)
            now[0] += 0.2
            if not chunk:
                proc.returncode = 0
            return chunk

        peer.send.side_effect = lambda chunk: (
            sent.append(bytes(chunk[:4096])) or min(4096, len(chunk))
        )
        with (
            spawned(sockets=[hook_server, display_server], proc=proc),
            mock.patch.object(os, "waitpid", side_effect=ChildProcessError),
            mock.patch.object(os, "read", side_effect=read),
            mock.patch.object(os, "write", side_effect=lambda _fd, chunk: len(chunk)),
            mock.patch.object(guard.time, "monotonic", side_effect=lambda: now[0]),
            mock.patch.object(
                guard.select,
                "select",
                side_effect=lambda r, w, _e, _t: ([20] if 20 in r else [], w, []),
            ),
        ):
            self.assertEqual(runtime.run(), 0)
        self.assertEqual(b"".join(sent), data)


class GuardTest(Base):
    def test_wide_character_at_right_margin_and_combining_mark_wrap_safely(self):
        screen = guard.Screen()
        screen.append(("a" * 79 + "中\u0301").encode())
        self.assertEqual(screen.text(None), "a" * 79 + "\n中\u0301")
        self.assertTrue(all(len(row) == 80 for row in screen.cells))

    def test_size_parses_columns_x_rows_into_rows_and_columns(self):
        self.assertEqual(guard.size("100x30"), (30, 100))
        self.assertEqual(guard.size("65535x1"), (1, 65535))
        for value in ("0x24", "80x0", "-80x24", "80x-24", "80.5x24", "80", "axb", "80x24x1"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                guard.size(value)
        with self.assertRaises(argparse.ArgumentTypeError):
            guard.size("65536x24")

    def test_size_defaults_to_80x24_and_parses_the_given_one(self):
        command = ["--hook-socket", self.home + "/hook", "--", "true"]
        self.assertEqual(guard.arguments().parse_args(command).size, (24, 80))
        self.assertEqual(
            guard.arguments().parse_args(["--size", "100x30", *command]).size, (30, 100)
        )

    def test_an_invalid_size_is_a_usage_error(self):
        with (
            mock.patch.object(sys, "stderr", io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as raised,
        ):
            guard.main(["--size", "0x24", "--hook-socket", self.home + "/hook", "--", "true"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--size", stderr.getvalue())

    def test_the_pty_and_screen_start_at_the_given_size(self):
        runtime = self.runtime(terminal=True, size=(30, 100))
        self.assertEqual((runtime.screen.rows, runtime.screen.columns), (30, 100))
        with spawned() as (ioctl, _, _):
            runtime.start()
        sized = [c for c in ioctl.call_args_list if c.args[1] == termios.TIOCSWINSZ]
        self.assertEqual([struct.unpack("HHHH", c.args[2]) for c in sized], [(30, 100, 0, 0)])

    def test_pending_uninspectable_child_has_a_bounded_close_deadline(self):
        runtime = self.runtime()
        runtime.proc = mock.Mock(pid=10, returncode=0)
        runtime.closing = 0
        with (
            mock.patch.object(os, "listdir", return_value=[]),
            mock.patch.object(os, "waitpid", return_value=(0, 0)),
            mock.patch.object(guard.time, "monotonic", return_value=10),
            self.assertRaisesRegex(OSError, "exit unconfirmed"),
        ):
            runtime.tick()

    def test_detach_ready_during_output_cannot_end_worker(self):
        runtime = self.runtime(terminal=True)
        peer = mock.Mock()
        peer.send.return_value = 0
        peer.recv.return_value = b""
        runtime.peers[peer] = ("display", guard.Decoder())
        proc = mock.Mock(returncode=0, pid=100)
        captured = []
        with (
            spawned(proc=proc) as (_, _, sockets),
            mock.patch.object(os, "waitpid", side_effect=ChildProcessError),
            mock.patch.object(
                guard.select, "select", side_effect=[([20, peer], [], []), ([20], [], [])]
            ),
            mock.patch.object(os, "read", side_effect=[b"prompt", b""]),
            mock.patch.object(
                os, "write", side_effect=lambda _fd, data: captured.append(data) or len(data)
            ),
        ):
            sockets.return_value.accept.side_effect = BlockingIOError
            self.assertEqual(runtime.run(), 0)
        self.assertEqual(
            [kind for kind, _ in guard.Decoder().feed(b"".join(captured))], [b"s", b"x"]
        )

    def test_new_display_receives_existing_idle_screen(self):
        runtime = self.runtime(terminal=True)
        runtime.args.display_socket = self.home + "/display"
        hook_server, display_server, peer = mock.Mock(), mock.Mock(), mock.Mock()
        hook_server.accept.side_effect = BlockingIOError
        display_server.accept.return_value = (peer, "")
        peer.send.side_effect = len
        proc = mock.Mock(returncode=0, pid=100)
        with (
            spawned(sockets=[hook_server, display_server], proc=proc),
            mock.patch.object(os, "waitpid", side_effect=ChildProcessError),
            mock.patch.object(
                guard.select,
                "select",
                side_effect=[([20], [], []), ([display_server], [], []), ([20], [], [])],
            ),
            mock.patch.object(os, "read", side_effect=[b"idle prompt", b""]),
            mock.patch.object(os, "write", side_effect=lambda _fd, data: len(data)),
        ):
            self.assertEqual(runtime.run(), 0)
        self.assertIn(b"idle prompt", peer.send.call_args.args[0])

    def test_screen_erased_dialog_does_not_survive_in_visible_text(self):
        screen = guard.Screen(rows=3, columns=20)
        screen.append(b"permission question\x1b[2J\x1b[Haccepted")
        self.assertEqual(screen.text(None), "accepted")
        self.assertNotIn("permission", screen.snapshot().decode())
        screen.append(b"\r\x1b[2Kdone")
        self.assertEqual(screen.text(None), "done")

    def test_screen_cursor_overwrite_insert_delete_and_restore(self):
        screen = guard.Screen(rows=3, columns=8)
        screen.append(b"abcd\x1b[1;2H\x1b[P\x1b[2@X")
        self.assertEqual(screen.text(None), "aX cd")
        screen.append(b"\x1b[s\x1b[3;1Hz\x1b[uY")
        self.assertEqual(screen.text(None), "aXYcd\n\nz")
        screen.append(b"\x1b[1A\x1b[1B\x1b[1C\x1b[1D\x1b[2d\x1b[3GQ")
        self.assertEqual(screen.text(None), "aXYcd\n  Q\nz")
        screen.append(b"\x1b[1J")
        self.assertEqual(screen.text(None), "\n\nz")

    def test_screen_scrollback_is_separate_and_alternate_screen_restores(self):
        screen = guard.Screen(rows=2, columns=8)
        screen.append(b"a\r\nb\r\nc")
        self.assertEqual(screen.text(None), "b\nc")
        self.assertEqual(screen.text(3), "a\nb\nc")
        screen.append(b"\x1b[?1049halt\x1b[?1049l")
        self.assertEqual(screen.text(None), "b\nc")
        screen.append(b"\x1b[3J")
        self.assertEqual(screen.text(3), "")

    def test_screen_split_unicode_osc_and_character_sets(self):
        screen = guard.Screen(rows=2, columns=8)
        wire = "\x1b]0;title\x07\x1b(B中é\rA\bB\tQ".encode()
        for byte in wire:
            screen.append(bytes([byte]))
        self.assertNotIn("title", screen.text(None))
        self.assertNotIn("\x1b", screen.text(None))
        self.assertTrue(screen.text(None).startswith("B"))
        self.assertIn("Q", screen.text(None))

    def test_screen_swallows_every_string_sequence_however_long(self):
        """The client's own kitty-graphics query (APC) once showed as prompt text (WDRC-344)."""
        screen = guard.Screen(rows=2, columns=40)
        screen.append(b"> \x1b_Gi=31,s=1,v=1,a=q,t=d,f=24;AAAA\x1b\\typed")
        self.assertEqual(screen.text(None), "> typed")
        for start, end in ((b"\x1bP", b"\x1b\\"), (b"\x1b^", b"\x1b\\"), (b"\x1bX", b"\x07")):
            with self.subTest(start=start):
                screen = guard.Screen(rows=2, columns=40)
                screen.append(start + b"+q544e" + end + b"ok")
                self.assertEqual(screen.text(None), "ok")
        screen = guard.Screen(rows=2, columns=40)
        for part in (b"\x1b]8;;https://example.com/", b"x" * 1000, b"\x1b", b"\\link"):
            screen.append(part)
        self.assertEqual(screen.text(None), "link")
        self.assertLess(len(screen.escape), 4)

    def test_live_client_adopted_zombie_is_reaped_and_direct_exit_code_kept(self):
        runtime = self.runtime()
        runtime.proc = mock.Mock(pid=10, returncode=None)
        with mock.patch.object(os, "waitpid", side_effect=[(12, 0), (0, 0)]) as waited:
            runtime.reap()
        self.assertEqual(waited.call_count, 2)
        self.assertIsNone(runtime.proc.returncode)
        with mock.patch.object(os, "waitpid", side_effect=[(10, 7 << 8), ChildProcessError]):
            runtime.reap()
        self.assertEqual(runtime.proc.returncode, 7)

    def test_uninspectable_tracked_child_cannot_be_confirmed_dead(self):
        with (
            mock.patch.object(os, "listdir", return_value=["12"]),
            mock.patch.object(Path, "read_text", side_effect=PermissionError),
        ):
            self.assertEqual(guard.processes(), {})
            with self.assertRaises(PermissionError):
                guard.processes({12: "original"})

    def test_late_adoption_after_snapshot_prevents_false_tree_exit(self):
        runtime = self.runtime()
        runtime.proc = mock.Mock(pid=10, returncode=0)
        with (
            mock.patch.object(os, "listdir", return_value=[]),
            mock.patch.object(os, "waitpid", return_value=(0, 0)),
        ):
            self.assertFalse(runtime.tick())

    def attached(self, *, terminal, ready, sizes=()):
        """guard.main --attach: (what it sent the guard, what it wrote out, restore, raw) with
        stdin and the SIGWINCH pipe (30) read in ready's order.
        """
        peer = mock.Mock()
        peer.recv.side_effect = [b"terminal output", b""]
        output = io.BytesIO()
        reads = {0: b"typed", 30: b"\x1c"}
        sizes = iter(sizes)

        def size(fd):
            if fd != 0:
                raise OSError(errno.ENOTTY, "not a terminal")
            return next(sizes)

        with (
            mock.patch.object(guard.socket, "socket", return_value=peer),
            mock.patch.object(os, "isatty", return_value=terminal),
            mock.patch.object(termios, "tcgetattr", return_value=["saved"]),
            mock.patch.object(termios, "tcsetattr") as restore,
            mock.patch.object(tty, "setraw") as raw,
            mock.patch.object(os, "pipe", return_value=(30, 31)),
            mock.patch.object(os, "set_blocking"),
            mock.patch.object(os, "close"),
            mock.patch.object(signal, "signal", return_value=signal.SIG_DFL) as handlers,
            mock.patch.object(signal, "set_wakeup_fd", return_value=-1) as wakeup,
            mock.patch.object(os, "get_terminal_size", side_effect=size),
            mock.patch.object(
                guard.select,
                "select",
                side_effect=[([fd if fd != "peer" else peer], [], []) for fd in ready],
            ),
            mock.patch.object(os, "read", side_effect=lambda fd, _size: reads[fd]),
            mock.patch.object(sys, "stdout", buffer=output),
        ):
            self.assertEqual(guard.main(["--attach", self.home + "/display"]), 0)
        self.assertEqual(wakeup.call_args_list, [mock.call(31), mock.call(-1)])
        self.assertEqual(handlers.call_args_list[-1], mock.call(signal.SIGWINCH, signal.SIG_DFL))
        peer.close.assert_called_once()
        sent = guard.Decoder().feed(b"".join(c.args[0] for c in peer.sendall.call_args_list))
        return sent, output.getvalue(), restore, raw

    def test_display_attach_sends_its_size_typing_and_each_resize(self):
        sent, output, restore, raw = self.attached(
            terminal=True,
            ready=[0, 30, "peer", "peer"],
            sizes=[os.terminal_size((33, 19)), os.terminal_size((80, 24))],
        )
        self.assertEqual(
            sent,
            [
                (b"w", b'{"rows":19,"columns":33}'),
                (b"i", b"typed"),
                (b"w", b'{"rows":24,"columns":80}'),
            ],
        )
        self.assertEqual(output, b"terminal output")
        raw.assert_called_once_with(0, termios.TCSANOW)  # keeps what was typed before it
        restore.assert_called_once_with(0, termios.TCSADRAIN, ["saved"])

    def test_display_attach_without_a_terminal_sends_no_size(self):
        sent, output, restore, raw = self.attached(terminal=False, ready=[0, 30, "peer", "peer"])
        self.assertEqual((sent, output), ([(b"i", b"typed")], b"terminal output"))
        raw.assert_not_called()
        restore.assert_not_called()

    def test_a_missing_display_socket_is_one_line_not_a_traceback(self):
        peer = mock.Mock()
        peer.connect.side_effect = FileNotFoundError(errno.ENOENT, "No such file or directory")
        err = io.StringIO()
        with (
            mock.patch.object(guard.socket, "socket", return_value=peer),
            mock.patch.object(sys, "stderr", err),
        ):
            self.assertEqual(guard.main(["--attach", self.home + "/display"]), 1)
        self.assertEqual(
            err.getvalue(), f"guard: display {self.home}/display: No such file or directory\n"
        )
        peer.close.assert_called_once()

    def test_a_client_dead_before_any_display_lingers_for_its_first_one(self):
        """A --display-grace terminal guard waits DISPLAY_GRACE s after the exit for a display."""
        runtime = self.runtime(terminal=True, display_grace=True)
        runtime.exit_at = 100.0
        self.assertFalse(runtime.lingers(100.5))  # headless: no PTY
        runtime.master = 20
        self.assertTrue(runtime.lingers(100.5))
        runtime.args.display_grace = False
        self.assertFalse(runtime.lingers(100.5))  # no display starts with it
        runtime.args.display_grace = True
        self.assertFalse(runtime.lingers(100.0 + guard.DISPLAY_GRACE))
        server, peer = mock.Mock(), mock.Mock()
        server.accept.side_effect = BlockingIOError
        runtime.take_display(server)
        self.assertTrue(runtime.lingers(100.5))
        runtime.screen.append(b"claude: error: unrecognized arguments: --bogus")
        server.accept.side_effect, server.accept.return_value = None, (peer, "")
        peer.send.side_effect = len
        runtime.take_display(server)
        self.assertIn(b"unrecognized arguments", peer.send.call_args.args[0])
        self.assertFalse(runtime.lingers(100.5))

    def test_a_close_with_no_display_ever_connected_skips_the_grace(self):
        """An owned Docker tui worker nobody viewed: no --display-grace, so a close ends at once."""
        self.assertEqual(self.closed_without_display(display_grace=False), 0)
        with self.assertRaisesRegex(AssertionError, "lingered"):
            self.closed_without_display(display_grace=True)

    def closed_without_display(self, *, display_grace):
        """A terminal guard told to close before any display connected, the clock frozen."""
        runtime = self.runtime(terminal=True, display_grace=display_grace)
        runtime.args.display_socket = self.home + "/display"
        hook_server, display_server = mock.Mock(), mock.Mock()
        hook_server.accept.side_effect = BlockingIOError
        display_server.accept.side_effect = BlockingIOError
        proc = mock.Mock(returncode=None, pid=100)
        with (
            spawned(sockets=[hook_server, display_server], proc=proc),
            mock.patch.object(os, "waitpid", side_effect=ChildProcessError),
            mock.patch.object(guard.select, "select", side_effect=[([0], [], []), ([20], [], [])]),
            mock.patch.object(os, "read", side_effect=[guard.frame(b"c", b"{}"), b""]),
            mock.patch.object(os, "write", side_effect=lambda _fd, data: len(data)),
            mock.patch.object(guard.time, "monotonic", return_value=100.0),
            mock.patch.object(
                guard.time, "sleep", side_effect=AssertionError("lingered for a display")
            ),
        ):
            proc.poll.side_effect = lambda: setattr(proc, "returncode", 0)
            return runtime.run()

    def test_macos_kernel_process_identity_and_enumeration(self):
        library = mock.Mock()

        def info(_pid, _kind, _arg, data, _size):
            struct.pack_into("=I", data, 4, 2)
            struct.pack_into("=I", data, 16, 5)
            struct.pack_into("=QQ", data, 120, 123, 456)
            return 136

        def listing(_kind, _arg, slots, _size):
            if slots is None:
                return 4
            slots[0] = 9
            return 4

        library.proc_pidinfo.side_effect = info
        library.proc_listpids.side_effect = listing
        with (
            mock.patch.object(sys, "platform", "darwin"),
            mock.patch.object(guard.ctypes, "CDLL", return_value=library),
            mock.patch.object(os, "kill") as killed,
        ):
            self.assertEqual(guard.process_info(9), (5, "123:456", False))
            self.assertEqual(guard.processes(), {9: (5, "123:456", False)})
            guard.signal_process(9, "123:456", signal.SIGKILL)
            killed.assert_called_once_with(9, signal.SIGKILL)
            guard.subreaper()
        library.proc_pidinfo.side_effect = None
        library.proc_pidinfo.return_value = 0
        with (
            mock.patch.object(sys, "platform", "darwin"),
            mock.patch.object(guard.ctypes, "CDLL", return_value=library),
            mock.patch.object(guard.ctypes, "get_errno", return_value=errno.ESRCH),
        ):
            self.assertIsNone(guard.process_info(9))

    def test_missing_linux_process_and_unreadable_mac_inventory(self):
        with mock.patch.object(Path, "read_text", side_effect=FileNotFoundError):
            self.assertIsNone(guard.process_info(9))
        with (
            mock.patch.object(sys, "platform", "darwin"),
            mock.patch.object(guard.ctypes, "CDLL") as library,
        ):
            library.return_value.proc_listpids.return_value = 0
            with self.assertRaises(OSError):
                guard.processes()
            library.return_value.proc_pidinfo.return_value = 0
            with (
                mock.patch.object(guard.ctypes, "get_errno", return_value=errno.EPERM),
                self.assertRaises(PermissionError),
            ):
                guard.process_info(9)

    def test_child_clears_signal_mask_and_claims_controlling_terminal(self):
        with (
            mock.patch.object(signal, "pthread_sigmask") as mask,
            mock.patch.object(guard.fcntl, "ioctl") as ioctl,
        ):
            guard.child_setup(9)
        mask.assert_called_once_with(signal.SIG_SETMASK, ())
        ioctl.assert_called_once_with(9, termios.TIOCSCTTY, 0)

    def test_guard_control_output_has_a_hard_bound(self):
        runtime = self.runtime()
        with self.assertRaises(ValueError):
            runtime.emit(b"x" * (guard.BUFFER_MAX + 1))
        self.assertFalse(runtime.output)

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
        self.assertLessEqual(len(screen.text(None)), 32)

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

    def test_a_hook_while_closing_is_acked_once_its_whole_frame_is_written(self):
        runtime = self.runtime()
        runtime.close()  # stdin no longer read: no k to wait for
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

    def test_a_hook_is_acked_only_once_its_harness_took_it(self):
        runtime = self.runtime()
        peers = []
        for delivery in ("a" * 32, "b" * 32):
            peer = mock.Mock()
            value = {"label": "Stop", "payload": {}, "delivery_id": delivery}
            peer.recv.return_value = json.dumps(value).encode() + b"\n"
            runtime.peers[peer] = ("hook", bytearray())
            runtime.peer_read(peer)
            peers.append(peer)
        with mock.patch.object(os, "write", side_effect=lambda _fd, wire: len(wire)):
            runtime.flush()
        self.assertFalse(runtime.output)
        for peer in peers:
            peer.sendall.assert_not_called()
        runtime.command(b"k", b"{}")
        self.assertEqual(
            json.loads(peers[0].sendall.call_args.args[0]), {"ok": True, "delivery_id": "a" * 32}
        )
        peers[0].close.assert_called_once()
        peers[1].sendall.assert_not_called()
        runtime.command(b"k", b"{}")
        self.assertEqual(
            json.loads(peers[1].sendall.call_args.args[0]), {"ok": True, "delivery_id": "b" * 32}
        )
        runtime.command(b"k", b"{}")  # none left: ignored
        with self.assertRaises(ValueError):
            runtime.command(b"k", b"[]")

    def test_closing_acks_every_hook_still_waiting_for_its_take(self):
        runtime = self.runtime()
        peer = mock.Mock()
        runtime.peers[peer] = ("ack", bytearray())
        runtime.taken.append((peer, "a" * 32))
        runtime.close()
        self.assertEqual(
            json.loads(peer.sendall.call_args.args[0]), {"ok": True, "delivery_id": "a" * 32}
        )
        self.assertFalse(runtime.taken)

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
        with self.assertRaises(ValueError):
            runtime.command(b"i", b"x" * (guard.BUFFER_MAX + 1))
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
        peer.recv.return_value = guard.frame(b"i", b"type")
        runtime.peers[peer] = ("display", guard.Decoder())
        runtime.peer_read(peer)
        self.assertEqual(runtime.input, b"type")
        peer.recv.return_value = guard.frame(b"i", b"x" * guard.BUFFER_MAX)
        runtime.peer_read(peer)
        self.assertNotIn(peer, runtime.peers)
        self.assertEqual(runtime.input, b"type")

    def display(self, runtime):
        peer = mock.Mock()
        runtime.peers[peer] = ("display", guard.Decoder())
        return peer

    def test_the_pty_takes_the_latest_display_size_and_without_one_the_given_one(self):
        runtime = self.runtime(terminal=True, size=(30, 100))
        runtime.master = 20
        first, second = self.display(runtime), self.display(runtime)

        def sized(peer, rows, columns, extra=b""):
            peer.recv.return_value = (
                guard.json_frame(b"w", {"rows": rows, "columns": columns}) + extra
            )
            runtime.peer_read(peer)

        with mock.patch.object(guard.fcntl, "ioctl") as ioctl:
            sized(first, 19, 33, guard.frame(b"i", b"typed"))
            sized(second, 50, 200)
            sized(first, 20, 34)  # a resize is the latest too
            runtime.drop(first)
            sized(second, 50, 200)  # unchanged: no redraw
            runtime.drop(second)
        self.assertEqual(
            [
                (c.args[0], c.args[1], struct.unpack("HHHH", c.args[2]))
                for c in ioctl.call_args_list
            ],
            [
                (20, termios.TIOCSWINSZ, (rows, columns, 0, 0))
                for rows, columns in ((19, 33), (50, 200), (20, 34), (50, 200), (30, 100))
            ],
        )
        self.assertEqual(runtime.input, b"typed")
        runtime.screen.append(b"x" * 100)
        runtime.command(b"q", b'{"lines": null}')
        self.assertEqual(json.loads(runtime.output[-1][0][5:]), {"text": "x" * 100})

    def test_a_display_closing_with_its_client_redraws_nothing(self):
        runtime = self.runtime(terminal=True)
        runtime.master = 20
        peer = self.display(runtime)
        peer.recv.return_value = guard.json_frame(b"w", {"rows": 19, "columns": 33})
        with mock.patch.object(guard.fcntl, "ioctl") as ioctl:
            runtime.peer_read(peer)
            runtime.close()
            runtime.drop(peer)
        self.assertEqual(ioctl.call_count, 1)

    def test_an_invalid_display_frame_drops_only_that_display(self):
        for wire in (
            b"typed by an old display",
            guard.frame(b"z", b""),
            guard.frame(b"w", b"not json"),
            guard.json_frame(b"w", {"rows": 19}),
            guard.json_frame(b"w", {"rows": 0, "columns": 33}),
            guard.json_frame(b"w", {"rows": True, "columns": 33}),
            guard.json_frame(b"w", {"rows": 19, "columns": 65536}),
            guard.json_frame(b"w", {"rows": 19, "columns": 33, "x": 1}),
            guard.frame(b"i", b"x")[:5] + b"x" * (guard.BUFFER_MAX + 1),
        ):
            with self.subTest(wire=wire[:30]):
                runtime = self.runtime(terminal=True)
                runtime.master = 20
                peer, other = self.display(runtime), self.display(runtime)
                peer.recv.return_value = wire
                with mock.patch.object(guard.fcntl, "ioctl") as ioctl:
                    runtime.peer_read(peer)
                self.assertEqual(list(runtime.peers), [other])
                ioctl.assert_not_called()
                self.assertEqual(runtime.input, b"")

    def test_screen_resize_keeps_lines_and_the_cursor_line(self):
        screen = guard.Screen(rows=3, columns=10)
        screen.append(b"one\r\ntwo\r\nthree")
        screen.resize(2, 4)
        self.assertEqual((screen.text(None), screen.text(3)), ("two\nthre", "one\ntwo\nthre"))
        self.assertEqual((screen.row, screen.column), (1, 4))
        screen.resize(4, 12)
        screen.append(b"e!")
        self.assertEqual(screen.text(None), "two\nthree!")
        self.assertEqual([len(line) for line in screen.cells], [12] * 4)
        screen = guard.Screen(rows=3, columns=10)
        screen.append(b"main\x1b[?1049halt")
        screen.resize(2, 6)
        screen.append(b"\x1b[?1049l")
        self.assertEqual(screen.text(None), "main")
        self.assertEqual([len(line) for line in screen.cells], [6] * 2)

    def test_terminal_output_keeps_short_writes_for_the_next_writable_event(self):
        runtime = self.runtime()
        peer = mock.Mock()
        peer.send.side_effect = [1, 4]
        runtime.peers[peer] = ("display", guard.Decoder())
        runtime.displayed = True
        runtime.streams[8] = b"t"
        with mock.patch.object(os, "read", return_value=b"hello"):
            runtime.read_stream(8)
        runtime.write_display(peer)
        runtime.write_display(peer)
        self.assertEqual(runtime.screen.text(None), "hello")
        self.assertIn(peer, runtime.peers)
        self.assertEqual(peer.send.call_args_list, [mock.call(b"hello"), mock.call(b"ello")])
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

    def test_an_empty_input_frame_closes_stdin_after_the_queued_bytes(self):
        runtime = self.runtime()
        runtime.proc, runtime.input_fd = mock.Mock(), 10
        runtime.command(b"i", b"queued")
        runtime.command(b"i", b"")
        runtime.command(b"i", b"late")
        written = []
        with mock.patch.object(
            os, "write", side_effect=lambda _fd, data: written.append(bytes(data)) or 3
        ):
            runtime.write_input(ended=False)
            runtime.proc.stdin.close.assert_not_called()
            runtime.write_input(ended=False)
            runtime.write_input(ended=False)
        self.assertEqual(written, [b"queued", b"ued"])
        runtime.proc.stdin.close.assert_called_once()

    def test_a_terminal_guard_ignores_an_empty_input_frame(self):
        runtime = self.runtime(terminal=True)
        runtime.proc, runtime.master, runtime.input_fd = mock.Mock(), 8, 8
        runtime.command(b"i", b"")
        runtime.command(b"i", b"typed")
        written = []
        with mock.patch.object(
            os, "write", side_effect=lambda _fd, data: written.append(bytes(data)) or len(data)
        ):
            runtime.write_input(ended=False)
        self.assertEqual(written, [b"typed"])
        runtime.proc.stdin.close.assert_not_called()

    def test_input_queued_at_the_clients_exit_is_dropped(self):
        runtime = self.runtime()
        runtime.proc, runtime.input_fd = mock.Mock(), 10
        runtime.command(b"i", b"queued")
        runtime.command(b"i", b"")
        with mock.patch.object(os, "write") as write:
            runtime.write_input(ended=True)
        write.assert_not_called()
        runtime.proc.stdin.close.assert_not_called()

    def test_pidfd_prevents_recycled_pid_signal(self):
        with (
            mock.patch.object(os, "pidfd_open", return_value=9),
            mock.patch.object(os, "close") as closed,
            mock.patch.object(
                Path, "read_text", return_value="8 (fake) S 1 8 8 " + "0 " * 15 + "new 0"
            ),
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
            mock.patch.object(os, "listdir", return_value=[]),
            mock.patch.object(os, "waitpid", side_effect=ChildProcessError),
        ):
            self.assertTrue(runtime.tick())

    def up_frames(self, returncode: int | None, now: float) -> list[bytes]:
        """The u frames two ticks at `now` emit for a client spawned at 100, reaped as
        `returncode` (None: still running).
        """
        runtime = self.runtime()
        runtime.proc = mock.Mock(pid=10, returncode=None)
        runtime.spawned = 100.0
        reaped = (0, 0) if returncode is None else (10, returncode << 8)
        with (
            mock.patch.object(os, "listdir", return_value=[]),
            mock.patch.object(os, "waitpid", side_effect=[reaped, ChildProcessError] * 2),
            mock.patch.object(guard.time, "monotonic", return_value=now),
        ):
            runtime.tick()
            runtime.tick()
        return [wire for wire, _, _ in runtime.output if wire[:1] == b"u"]

    def test_a_client_running_up_seconds_after_exec_gets_one_up(self):
        self.assertEqual(self.up_frames(None, 100 + guard.UP), [guard.json_frame(b"u", {})])

    def test_a_client_running_less_than_up_seconds_gets_no_up_yet(self):
        self.assertEqual(self.up_frames(None, 100 + guard.UP - 0.1), [])

    def test_a_client_that_exited_before_up_never_gets_up_however_late_its_guard_looks(self):
        self.assertEqual(self.up_frames(2, 100 + guard.UP + 60), [])

    def test_up_is_timed_by_the_clock_read_before_its_reap(self):
        """Running when reaped just under UP: it may die before a later clock read says UP."""
        runtime = self.runtime()
        runtime.proc = mock.Mock(pid=10, returncode=None)
        runtime.spawned = 100.0
        reaped = []
        answers = iter([(0, 0), (10, 2 << 8), ChildProcessError()])  # its next reap: exited

        def waitpid(*_):
            reaped.append(True)
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

        with (
            mock.patch.object(os, "listdir", return_value=[]),
            mock.patch.object(os, "waitpid", side_effect=waitpid),
            mock.patch.object(
                guard.time,
                "monotonic",
                side_effect=lambda: 100 + guard.UP + (len(reaped) * 60 if reaped else -0.001),
            ),
        ):
            runtime.tick()
            runtime.tick()
        self.assertEqual(runtime.proc.returncode, 2)
        self.assertEqual([wire for wire, _, _ in runtime.output if wire[:1] == b"u"], [])

    def test_stopped_child_requires_escalation_and_no_false_confirmation(self):
        runtime = self.runtime()
        runtime.proc = mock.Mock(returncode=None)
        runtime.known = {10: "123"}
        runtime.closing = 1
        with (
            mock.patch.object(os, "listdir", return_value=["10"]),
            mock.patch.object(
                Path, "read_text", return_value="10 (fake) S 1 10 10 " + "0 " * 15 + "123 0"
            ),
            mock.patch.object(guard.time, "monotonic", return_value=3),
            mock.patch.object(os, "pidfd_open", return_value=9),
            mock.patch.object(os, "close"),
            mock.patch.object(signal, "pidfd_send_signal") as sent,
        ):
            self.assertFalse(runtime.tick())
            self.assertFalse(runtime.tick())
        sent.assert_called_once_with(9, signal.SIGKILL)
        with (
            mock.patch.object(os, "listdir", return_value=["10"]),
            mock.patch.object(
                Path, "read_text", return_value="10 (fake) S 1 10 10 " + "0 " * 15 + "123 0"
            ),
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
        with spawned(terminal=False) as (_, popen, _), self.assertRaises(FileNotFoundError):
            popen.side_effect = FileNotFoundError
            runtime.start()
        self.assertFalse(runtime.output)

    def started_env(self, *, terminal, **environ):
        """The env a started guard gave its client, under environ."""
        runtime = self.runtime(terminal=terminal)
        with mock.patch.dict(os.environ, environ), spawned() as (_, spawn, _):
            if "TERM" not in environ:
                os.environ.pop("TERM", None)
            runtime.start()
        return spawn.call_args.kwargs["env"]

    def test_a_pty_client_without_term_gets_a_color_term(self):
        """A Docker client inherits no TERM: without one it drew no color (WDRC-344)."""
        env = self.started_env(terminal=True, KEEP="1")
        self.assertEqual((env["TERM"], env["KEEP"]), ("xterm-256color", "1"))
        self.assertEqual(self.started_env(terminal=True, TERM="screen")["TERM"], "screen")
        self.assertIsNone(self.started_env(terminal=False))

    def test_run_places_exit_after_last_output(self):
        runtime = self.runtime()
        proc = mock.Mock(returncode=0, pid=100)
        proc.stdin.fileno.return_value = 10
        proc.stdout.fileno.return_value = 11
        proc.stderr.fileno.return_value = 12
        captured = []
        peer = mock.Mock()
        message = {"label": "last", "payload": {}, "delivery_id": "a" * 32}
        peer.recv.return_value = json.dumps(message).encode() + b"\n"
        runtime.peers[peer] = ("hook", bytearray())
        with (
            spawned(terminal=False, proc=proc) as (_, _, sockets),
            mock.patch.object(os, "waitpid", side_effect=ChildProcessError),
            mock.patch.object(
                guard.select, "select", side_effect=[([11, 12], [], []), ([11], [], [])]
            ),
            mock.patch.object(os, "read", side_effect=[b"last", b"", b"", b""]),
            mock.patch.object(
                os, "write", side_effect=lambda _fd, data: captured.append(data) or len(data)
            ),
        ):
            sockets.return_value.accept.side_effect = BlockingIOError
            self.assertEqual(runtime.run(), 0)
        self.assertEqual(
            [kind for kind, _ in guard.Decoder().feed(b"".join(captured))], [b"s", b"o", b"h", b"x"]
        )
        self.assertEqual(
            json.loads(peer.sendall.call_args.args[0]), {"ok": True, "delivery_id": "a" * 32}
        )


class LeaseTest(Case):
    def setUp(self):
        super().setUp()
        self.run = os.path.join(self.home, "run", "a" * 32)
        self.lease = os.path.join(self.run, "lease")

    def main(self):
        """A serve guard's start on this lease, its run dir's."""
        args, _, options = guard.launched({**HANDOFF, "argv": ["true"]}, b"")
        return guard.supervise(args, self.lease, **options)

    def test_argv_mode_takes_no_lease(self):
        with mock.patch.object(sys, "stderr", io.StringIO()), self.assertRaises(SystemExit):
            guard.main(["--hook-socket", self.run + "/hook", "--lease", self.lease, "--", "true"])

    def test_creates_its_private_dir_and_holds_the_lease_for_life(self):
        lease = guard.hold_lease(self.lease)
        assert lease is not None
        self.addCleanup(os.close, lease)
        self.assertEqual(os.stat(self.run).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.lease).st_mode & 0o777, 0o600)
        self.assertFalse(os.get_inheritable(lease))
        self.assertIsNone(guard.hold_lease(self.lease))
        args = argparse.Namespace(hook_socket=self.run + "/hook", lease=self.lease, size=guard.SIZE)
        runtime = guard.Guard(args, lease)
        self.assertEqual(runtime.directory, self.run)
        runtime.state("exited")
        with open(self.run + "/state.json") as stream:
            self.assertEqual(json.load(stream), {"generation": "a" * 32, "state": "exited"})

    def test_a_held_lease_exits_1(self):
        os.makedirs(self.run, 0o700)
        held = os.open(self.lease, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, held)
        guard.fcntl.flock(held, guard.fcntl.LOCK_EX)
        stderr = io.StringIO()
        with mock.patch.object(sys, "stderr", stderr):
            self.assertEqual(self.main(), 1)
        self.assertEqual(stderr.getvalue(), "guard: lease held\n")

    def test_a_symlinked_lease_is_refused(self):
        os.makedirs(self.run, 0o700)
        os.symlink(os.path.join(self.home, "elsewhere"), self.lease)
        with mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(self.main(), 1)
        self.assertFalse(os.path.exists(os.path.join(self.home, "elsewhere")))


class ProbeTest(Case):
    def setUp(self):
        super().setUp()
        self.run = os.path.join(self.home, "a" * 32)

    def probe(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            self.assertEqual(guard.main(["--probe", self.run]), 0)
        return out.getvalue()

    def write(self, state=None, *, lease=True):
        os.makedirs(self.run, 0o700, exist_ok=True)
        if lease:
            os.close(os.open(self.run + "/lease", os.O_RDWR | os.O_CREAT, 0o600))
        if state is not None:
            with open(self.run + "/state.json", "w") as stream:
                stream.write(state)

    def test_absent_dir(self):
        self.assertEqual(self.probe(), "absent\n")

    def test_held_lease_is_live(self):
        self.write('{"generation": "x", "state": "running"}')
        held = os.open(self.run + "/lease", os.O_RDWR)
        self.addCleanup(os.close, held)
        guard.fcntl.flock(held, guard.fcntl.LOCK_EX)
        self.assertEqual(self.probe(), "live\n")

    def test_free_lease_with_its_exited_state_is_exited(self):
        self.write(json.dumps({"generation": "a" * 32, "state": "exited"}))
        self.assertEqual(self.probe(), "exited\n")

    def test_anything_else_is_dead(self):
        for state, lease in (
            (json.dumps({"generation": "a" * 32, "state": "running"}), True),
            (json.dumps({"generation": "b" * 32, "state": "exited"}), True),
            ("{", True),
            (None, True),
            (json.dumps({"generation": "a" * 32, "state": "exited"}), False),
        ):
            with self.subTest(state=state, lease=lease):
                for name in ("lease", "state.json"):
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(os.path.join(self.run, name))
                self.write(state, lease=lease)
                self.assertEqual(self.probe(), "dead\n")


GEN = "a" * 32
HANDOFF = {
    "argv": ["client", "--flag"],
    "env": {"SECRET": "s3cret"},
    "terminal": True,
    "generation": GEN,
    "size": "100x30",
}


def handoff(**changes) -> bytes:
    return json.dumps({**HANDOFF, **changes}).encode()


class LaunchTest(Case):
    def test_a_valid_handoff_is_its_fields(self):
        self.assertEqual(guard.launch(handoff()), HANDOFF)
        self.assertEqual(guard.launch(handoff(terminal=False, env={}))["terminal"], False)

    def test_every_defect_is_one_message_without_its_content(self):
        defects = [
            b"not json s3cret",
            b"\xff",
            b"[]",
            json.dumps({k: v for k, v in HANDOFF.items() if k != "env"}).encode(),
            handoff(extra="s3cret"),
            handoff(argv=[]),
            handoff(argv="client"),
            handoff(argv=["cli\0ent"]),
            handoff(argv=[1]),
            handoff(env=[]),
            handoff(env={"": "s3cret"}),
            handoff(env={"A=B": "s3cret"}),
            handoff(env={"A\0": "s3cret"}),
            handoff(env={"A": "s3\0cret"}),
            handoff(env={"A": 1}),
            handoff(terminal=1),
            handoff(generation="A" * 32),
            handoff(generation="a" * 31),
            handoff(generation=1),
            handoff(size="80"),
            handoff(size="0x24"),
            handoff(size="65536x24"),
            handoff(size=[80, 24]),
        ]
        for data in defects:
            with self.subTest(data=data[:40]), self.assertRaises(ValueError) as caught:
                guard.launch(data)
            self.assertEqual(str(caught.exception), "invalid launch handoff")

    def await_launch(self, reads, *, clock=None):
        times = iter(clock or [0.0] * 20)
        with (
            mock.patch.object(guard.time, "monotonic", side_effect=lambda: next(times)),
            mock.patch.object(guard.select, "select", return_value=([0], [], [])),
            mock.patch.object(os, "read", side_effect=reads),
        ):
            return guard.await_launch(10.0)

    def test_the_first_frame_is_the_handoff_and_later_bytes_stay_pending(self):
        wire = guard.frame(b"l", handoff()) + guard.frame(b"p", b"{}") + b"i\x00"
        found, pending = self.await_launch([wire[:3], wire[3:]])
        self.assertEqual(found, HANDOFF)
        self.assertEqual(pending, guard.frame(b"p", b"{}") + b"i\x00")

    def test_another_first_frame_or_an_oversized_one_is_invalid(self):
        for wire in (guard.frame(b"p", b"{}"), b"l\xff\xff\xff\xff"):
            with (
                self.subTest(wire=wire),
                self.assertRaisesRegex(ValueError, "^invalid launch handoff$"),
            ):
                self.await_launch([wire])

    def test_eof_or_the_deadline_is_no_handoff(self):
        with self.assertRaisesRegex(ValueError, "^no launch handoff$"):
            self.await_launch([b""])
        with self.assertRaisesRegex(ValueError, "^no launch handoff$"):
            self.await_launch([b"l\x00"], clock=[0.0, 11.0])

    def test_the_deadline_bounds_each_wait(self):
        times = iter([0.0, 4.0, 10.0])
        with (
            mock.patch.object(guard.time, "monotonic", side_effect=lambda: next(times)),
            mock.patch.object(guard.select, "select", return_value=([], [], [])) as wait,
            mock.patch.object(os, "read") as read,
            self.assertRaisesRegex(ValueError, "^no launch handoff$"),
        ):
            guard.await_launch(10.0)
        self.assertEqual([call.args[3] for call in wait.call_args_list], [10.0, 6.0])
        read.assert_not_called()


class ServeTest(Case):
    def serve(self, reads, *, clock=(100.0, 100.0, 100.0)):
        """main(["--serve"]) over stdin `reads`: its code and stderr; it wrote and spawned nothing."""
        stderr, times = io.StringIO(), iter(clock)
        with (
            mock.patch.object(guard.time, "monotonic", side_effect=lambda: next(times)),
            mock.patch.object(guard.select, "select", return_value=([0], [], [])) as wait,
            mock.patch.object(os, "read", side_effect=reads),
            mock.patch.object(os, "write") as write,
            mock.patch.object(guard.subprocess, "Popen") as spawn,
            mock.patch.object(sys, "stderr", stderr),
        ):
            code = guard.main(["--serve"])
        write.assert_not_called()
        spawn.assert_not_called()
        return code, stderr.getvalue(), wait

    def test_the_handoff_wait_starts_with_the_guard(self):
        _, _, wait = self.serve([b""])
        self.assertEqual(wait.call_args.args[3], guard.LAUNCH_WAIT)

    def test_no_or_a_bad_handoff_exits_1_with_its_fixed_message_and_no_client(self):
        for reads, message in (
            ([b""], "no launch handoff"),
            ([guard.frame(b"p", b"{}")], "invalid launch handoff"),
            ([guard.frame(b"l", handoff(env={"A": 1}))], "invalid launch handoff"),
        ):
            with self.subTest(message=message):
                code, stderr, _ = self.serve(reads)
                self.assertEqual((code, stderr), (1, f"guard: {message}\n"))

    def test_a_handoff_runs_its_client_under_its_run_dir(self):
        args, lease, options = guard.launched(HANDOFF, b"pending")
        run = f"{guard.RUN}/{GEN}"
        self.assertEqual(
            vars(args),
            {
                "terminal": True,
                "hook_socket": f"{run}/hook",
                "display_socket": f"{run}/display",
                "display_grace": False,
                "size": (30, 100),
                "command": ["client", "--flag"],
            },
        )
        self.assertEqual(lease, f"{run}/lease")
        self.assertEqual(
            options,
            {
                "env": {"SECRET": "s3cret", "ENSEMBLE_GUARD_HOOK_SOCKET": f"{run}/hook"},
                "heartbeat": guard.HEARTBEAT,
                "pending": b"pending",
            },
        )

    def test_run_paths_are_the_ones_the_docker_host_names(self):
        run = f"/run/ensemble/guard/{GEN}"
        self.assertEqual(
            guard.run_paths(GEN),
            {
                "dir": run,
                "hook": f"{run}/hook",
                "lease": f"{run}/lease",
                "display": f"{run}/display",
            },
        )
        self.assertEqual(guard.HOOK_ENV, GUARD_HOOK_SOCKET)

    def test_a_headless_handoff_has_no_display_socket(self):
        args, _, _ = guard.launched({**HANDOFF, "terminal": False}, b"")
        self.assertIsNone(args.display_socket)

    def test_serve_takes_no_other_option(self):
        for argv in (["--terminal"], ["--hook-socket", "/x"], ["--", "client"]):
            with (
                self.subTest(argv=argv),
                mock.patch.object(sys, "stderr", io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                guard.main(["--serve", *argv])


class HeartbeatTest(Base):
    def test_a_serve_guard_closes_once_its_harness_is_silent_over_the_limit(self):
        runtime = self.runtime(heartbeat=5.0)
        runtime.heard = 100.0
        self.assertFalse(runtime.lost(105.0))
        self.assertTrue(runtime.lost(105.1))
        runtime.close()
        self.assertFalse(runtime.lost(200.0))
        self.assertFalse(self.runtime().lost(1e9))

    def test_every_channel_read_restarts_the_limit_and_feeds_its_frames(self):
        runtime = self.runtime(heartbeat=5.0)
        with (
            mock.patch.object(os, "read", side_effect=[b"i\x00\x00", b"\x00\x02hi"]),
            mock.patch.object(guard.time, "monotonic", side_effect=[50.0, 60.0]),
        ):
            runtime.read_channel()
            self.assertEqual((runtime.heard, runtime.input), (50.0, b""))
            runtime.read_channel()
        self.assertEqual((runtime.heard, runtime.input), (60.0, b"hi"))

    def test_a_silent_harness_closes_the_guard_only_with_no_bytes_waiting(self):
        runtime = self.runtime(heartbeat=5.0)
        runtime.heard = 0.0
        with mock.patch.object(guard.time, "monotonic", return_value=100.0):
            runtime.listen([0])
            self.assertIsNone(runtime.closing)
            runtime.listen([])
        self.assertEqual(runtime.closing, 100.0)

    def test_eof_on_the_channel_closes(self):
        runtime = self.runtime()
        with mock.patch.object(os, "read", return_value=b""):
            runtime.read_channel()
        self.assertIsNotNone(runtime.closing)

    def test_a_heartbeat_frame_is_accepted_and_does_nothing_else(self):
        runtime = self.runtime()
        runtime.command(b"p", b"{}")
        self.assertEqual((runtime.output, runtime.input, runtime.closing), (deque(), b"", None))
        with self.assertRaises(ValueError):
            runtime.command(b"p", b"[]")

    def test_pending_frames_from_the_handoff_read_are_handled_at_run(self):
        runtime = self.runtime(pending=guard.frame(b"i", b"early") + guard.frame(b"p", b"{}"))
        runtime.proc = mock.Mock(returncode=None)
        with (
            mock.patch.object(runtime, "start"),
            mock.patch.object(runtime, "tick", side_effect=OSError("stop")),
            mock.patch.object(guard.time, "monotonic", return_value=42.0),
            self.assertRaisesRegex(OSError, "stop"),
        ):
            runtime.run()
        self.assertEqual((runtime.input, runtime.heard), (b"early", 42.0))

    def test_the_handed_off_env_reaches_the_client_over_the_guards_own(self):
        runtime = self.runtime(env={"KEEP": "handoff", "NEW": "1"})
        runtime.args.terminal = True
        with (
            mock.patch.dict(os.environ, {"KEEP": "own", "OWN": "1"}),
            spawned() as (_, spawn, _),
        ):
            os.environ.pop("TERM", None)
            runtime.start()
        env = spawn.call_args.kwargs["env"]
        self.assertEqual(
            (env["KEEP"], env["NEW"], env["OWN"], env["TERM"]),
            ("handoff", "1", "1", "xterm-256color"),
        )
