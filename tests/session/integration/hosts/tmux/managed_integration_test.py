"""Real pipe/PTY ownership; synthetic conversation recovery, without client credentials."""

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from unittest import mock

from ensemble.session.engine.types import (
    Launch,
    Liveness,
    SessionBusy,
    Unanswered,
    WorkerExit,
    WorkerOutput,
)
from ensemble.session.harness import guard
from ensemble.session.hosts.tmux import TmuxHost, managed
from tests.session import guard_fixture, live_tmux
from tests.session.case import Case, child_env


class ManagedIntegrationTest(Case):
    def setUp(self):
        self.server = live_tmux.Server(self, self.home)
        self.server.start("manager")
        self.enterContext(mock.patch.dict(os.environ, self.server.env(), clear=True))
        self.host = TmuxHost("tmux")
        self.addCleanup(self.cleanup_children)

    def cleanup_children(self):
        for name in ("client", "child"):
            path = Path(self.home, name + ".pid")
            if path.exists():
                pid = int(path.read_text())
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)

    def alive(self, pid):
        info = guard.process_info(pid)
        return info is not None and not info[2]

    def worker(self, *, terminal=False):
        handle = self.host.worker(
            "managed-worker",
            Launch(
                (sys.executable, "-I", guard_fixture.__file__, "client", self.home), cwd=self.home
            ),
            terminal=terminal,
            metadata={"sid": "same-conversation"},
        )
        self.addCleanup(self.cleanup_worker, handle)
        return handle

    def cleanup_worker(self, handle):
        with contextlib.suppress(Unanswered):
            handle.close()
        if handle.proc.poll() is None:
            handle.proc.kill()
            handle.proc.wait(timeout=10)

    def crash_harness(self, *, terminal=False, daemon=False):
        proc = subprocess.Popen(
            [
                sys.executable,
                "-P",
                "-s",
                guard_fixture.__file__,
                "harness",
                self.home,
                *(["--terminal"] if terminal else []),
                *(["--daemon"] if daemon else []),
            ],
            env={**child_env(), **self.server.env()},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.addCleanup(self.cleanup_harness, proc)

        def started():
            if proc.poll() is not None:
                self.fail(f"harness exited {proc.returncode}: {proc.stderr.read().decode()}")
            return Path(self.home, "harness.json").exists()

        live_tmux.wait(started, what="guard start")
        live_tmux.wait(
            lambda: (
                Path(self.home, "client.pid").exists() and Path(self.home, "child.pid").exists()
            ),
            what="client tree",
        )
        value = json.loads(Path(self.home, "harness.json").read_text())
        pids = [int(Path(self.home, name + ".pid").read_text()) for name in ("client", "child")]
        return proc, value, pids

    def cleanup_harness(self, proc):
        """Its guard, once known, ended too: its last state write lands in the temp home."""
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        proc.stderr.close()
        try:
            guard_pid = json.loads(Path(self.home, "harness.json").read_text())["guard"]
        except (OSError, ValueError):
            return
        live_tmux.wait(lambda: not self.alive(guard_pid), what="the guard's end")

    def test_harness_sigkill_reclaims_stopped_detached_tree_and_allows_new_generation(self):
        proc, value, pids = self.crash_harness(terminal=True)
        os.kill(pids[0], signal.SIGSTOP)
        proc.kill()
        proc.wait(timeout=10)
        live_tmux.wait(lambda: not any(map(self.alive, pids)), what="stopped client tree gone")
        live_tmux.wait(
            lambda: (
                self.host.worker_liveness("managed-worker", value["generation"]) == Liveness.ABSENT
            ),
            what="confirmed old generation end",
        )
        worker = self.worker()
        self.assertNotEqual(worker.generation, value["generation"])
        worker.write(b"recall\n")
        output = bytearray()

        def recalled():
            for message in worker.read(0.1):
                if isinstance(message, WorkerOutput):
                    output.extend(message.data)
            return b"violet compass" in output

        live_tmux.wait(recalled, what="original synthetic conversation recalled")

    def test_linux_subreaper_catches_fast_setsid_daemon(self):
        if sys.platform != "linux":
            self.skipTest("macOS setsid daemon leakage is approved and documented")
        proc, value, pids = self.crash_harness(daemon=True)
        proc.kill()
        proc.wait(timeout=10)
        live_tmux.wait(lambda: not any(map(self.alive, pids)), what="adopted daemon reaped")

    def test_guard_sigkill_keeps_unknown_and_refuses_new_owner(self):
        worker = self.worker(terminal=True)
        live_tmux.wait(lambda: Path(self.home, "client.pid").exists(), what="client started")
        worker.proc.kill()
        worker.proc.wait(timeout=10)
        live_tmux.wait(lambda: worker.state() == Liveness.UNKNOWN, what="broken guard channel")
        pid = int(Path(self.home, "client.pid").read_text())
        live_tmux.wait(lambda: not self.alive(pid), what="guard death hangs up PTY client")
        self.assertEqual(
            self.host.worker_liveness(worker.name, worker.generation), Liveness.UNKNOWN
        )
        with self.assertRaises(SessionBusy):
            self.worker(terminal=True)

    def test_both_sigkill_close_pty_but_unconfirmed_generation_stays_unknown(self):
        proc, value, pids = self.crash_harness(terminal=True)
        os.kill(value["guard"], signal.SIGKILL)
        proc.kill()
        proc.wait(timeout=10)
        live_tmux.wait(lambda: not self.alive(pids[0]), what="PTY client hung up")
        # its PTY's hangup can precede the release of its lease: the guard's own end frees it
        live_tmux.wait(lambda: not self.alive(value["guard"]), what="the guard's end")
        self.assertEqual(
            self.host.worker_liveness("managed-worker", value["generation"]), Liveness.UNKNOWN
        )
        # Detached HUP-ignoring descendants intentionally demonstrate the absent OS backstop.
        for pid in pids:
            if self.alive(pid):
                os.kill(pid, signal.SIGKILL)

    def test_display_disconnect_does_not_end_worker(self):
        worker = self.worker(terminal=True)
        path = managed.location("tmux", worker.name) + "/" + worker.generation + "/display"
        for _ in range(2):
            with socket.socket(socket.AF_UNIX) as display:
                display.connect(path)
                display.sendall(b"recall\n")
            self.assertEqual(worker.state(), Liveness.LIVE)
        worker.close()
        self.assertEqual(worker.state(), Liveness.ABSENT)

    def test_natural_exit_delivers_final_output_before_one_exit(self):
        worker = self.worker()
        worker.write(b"exit\n")
        messages = []

        def exited():
            messages.extend(worker.read(0.1))
            return any(isinstance(message, WorkerExit) for message in messages)

        live_tmux.wait(exited, what="worker exit")
        self.assertIsInstance(messages[-1], WorkerExit)
        self.assertEqual(sum(isinstance(message, WorkerExit) for message in messages), 1)
        self.assertIn(
            b"final reply",
            b"".join(message.data for message in messages if isinstance(message, WorkerOutput)),
        )
        worker.close()
        worker.close()

    def test_end_of_input_delivers_the_bytes_before_it_then_eof(self):
        worker = self.worker()
        worker.write(b"recall\n")
        worker.write(b"")
        messages = []

        def exited():
            messages.extend(worker.read(0.1))
            return any(isinstance(message, WorkerExit) for message in messages)

        live_tmux.wait(exited, what="worker exit at stdin EOF")
        self.assertEqual(messages[-1], WorkerExit(0))
        self.assertIn(
            b"violet compass",
            b"".join(message.data for message in messages if isinstance(message, WorkerOutput)),
        )
        worker.close()

    def test_stalled_consumer_triggers_close_and_reader_keeps_draining(self):
        worker = self.worker()
        worker.write(b"flood\n")
        live_tmux.wait(lambda: worker.state() == Liveness.UNKNOWN, what="bounded queue overflow")
        live_tmux.wait(lambda: worker.proc.poll() is not None, what="overflow closes guard")
        worker.reader.join(timeout=10)
        self.assertFalse(worker.reader.is_alive())
