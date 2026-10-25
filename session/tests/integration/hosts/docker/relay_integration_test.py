"""The relay daemon, its connector and the stub over real Unix sockets in a temp run dir."""

import io
import json
import os
import socket
import threading
from unittest import mock

from ensemble.session.hosts.docker import relay, stub

from session.tests.case import Case

GEN = "0123456789abcdef0123456789abcdef"


class RelayTest(Case):
    def setUp(self):
        self.run = os.path.join(self.home, "run")
        os.mkdir(self.run)
        self.daemon = self.start()

    def start(self):
        daemon = relay.Daemon(GEN, self.run)
        self.assertTrue(daemon.start())
        self.addCleanup(daemon.close)
        return daemon

    def hook(self, label="Stop"):
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code = stub.main(["hook", "/tui/w", label], io.BytesIO(b'{"a": 1}'), self.daemon.socket)
        return code, err.getvalue()

    def connect(self):
        """A connector on pipes: (its thread, write end of its stdin, read stream of its stdout)."""
        stdin_r, stdin_w = os.pipe()
        stdout_r, stdout_w = os.pipe()
        thread = threading.Thread(
            target=relay.connect, args=(self.daemon.uplink, stdin_r, stdout_w), daemon=True
        )
        thread.start()
        out = os.fdopen(stdout_r, "rb")
        self.addCleanup(out.close)
        self.addCleanup(lambda: [os.close(fd) for fd in (stdin_r, stdout_w) if _open(fd)])
        return thread, stdin_w, out

    def test_a_second_daemon_exits_and_a_stale_socket_is_replaced(self):
        self.assertFalse(relay.Daemon(GEN, self.run).start())
        self.daemon.close()
        self.assertTrue(os.path.exists(self.daemon.socket))
        self.daemon = self.start()
        self.assertEqual(self.hook(), (0, ""))

    def test_stub_requests_queue_and_are_sent_one_at_a_time_and_replayed(self):
        for label in ("A", "B"):
            self.assertEqual(self.hook(label), (0, ""))
        thread, stdin, out = self.connect()
        first = relay.relay_request(out.readline())
        self.assertEqual((first["seq"], first["argv"][2]), (1, "A"))
        os.close(stdin)  # the supervisor goes before replying
        thread.join(5)
        self.assertFalse(thread.is_alive())
        thread, stdin, out = self.connect()
        self.assertEqual(relay.relay_request(out.readline())["seq"], 1)
        os.write(stdin, json.dumps({"gen": GEN, "seq": 1, "ok": True}).encode() + b"\n")
        second = relay.relay_request(out.readline())
        self.assertEqual((second["seq"], second["argv"][2]), (2, "B"))
        os.close(stdin)
        thread.join(5)

    def test_one_uplink_at_a_time(self):
        self.hook()
        _, first_in, first_out = self.connect()
        self.assertEqual(relay.relay_request(first_out.readline())["seq"], 1)
        late, late_in, late_out = self.connect()
        os.write(first_in, json.dumps({"gen": GEN, "seq": 1, "ok": True}).encode() + b"\n")
        self.hook()
        self.assertEqual(relay.relay_request(first_out.readline())["seq"], 2)
        os.close(first_in)
        self.assertEqual(relay.relay_request(late_out.readline())["seq"], 2)
        os.close(late_in)
        late.join(5)

    def test_connect_without_a_daemon_exits_1(self):
        self.daemon.close()
        os.unlink(self.daemon.uplink)
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            self.assertEqual(relay.connect(self.daemon.uplink, wait=0), 1)
        self.assertIn("relay connect", err.getvalue())

    def test_a_stub_without_a_daemon_exits_0_with_one_line(self):
        self.daemon.close()
        with socket.socket(socket.AF_UNIX) as probe:
            self.assertRaises(OSError, probe.connect, self.daemon.socket)
        code, err = self.hook()
        self.assertEqual(code, 0)
        self.assertEqual(len(err.splitlines()), 1)


def _open(fd):
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True
