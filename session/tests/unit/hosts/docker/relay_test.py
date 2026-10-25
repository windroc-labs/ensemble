"""hosts/docker/relay.py and stub.py: the wire parsers against the shared protocol fixtures, the queue,
stub answers, the uplink's stop-and-wait, and the stub's exits, over files and fake connections.
"""

import base64
import io
import json
import os
from unittest import mock

from ensemble.session.hosts.docker import relay, stub

from session.tests.case import REPO, Case

FIXTURES = os.path.join(REPO, "protocol", "docker-relay", "fixtures")
GEN = "0123456789abcdef0123456789abcdef"


def cases(definition):
    with open(os.path.join(FIXTURES, "index.json")) as f:
        return [c for c in json.load(f)["cases"] if c["definition"] == definition]


def line(case):
    with open(os.path.join(FIXTURES, case["file"])) as f:
        data = json.load(f)
    if "payload_bytes" in case:
        data["payload"] = base64.b64encode(bytes(case["payload_bytes"])).decode()
    return json.dumps(data).encode() + b"\n"


def request(seq_id=7, label="Stop"):
    found = {"id": seq_id, "argv": ["hook", "/tui/w", label], "payload": "e30="}
    return json.dumps(found).encode() + b"\n"


class Writer(io.BytesIO):
    def lines(self):
        return [json.loads(x) for x in self.getvalue().splitlines()]


class ParserTest(Case):
    def test_every_fixture_parses_as_its_index_says(self):
        for definition, parse in (
            ("stub_request", relay.stub_request),
            ("relay_request", relay.relay_request),
            ("relay_reply", relay.relay_reply),
        ):
            for case in cases(definition):
                with self.subTest(fixture=case["file"]):
                    self.assertEqual(parse(line(case)) is not None, case["valid"])

    def test_lines_need_their_newline_and_one_object_without_duplicates_or_constants(self):
        good = request()
        self.assertIsNotNone(relay.stub_request(good))
        for bad in (
            good.rstrip(b"\n"),
            good + b"{}\n",
            b'{"id": 1, "id": 2, "argv": ["hook", "/t", "S"], "payload": ""}\n',
            b'{"id": NaN, "argv": ["hook", "/t", "S"], "payload": ""}\n',
            b'{"id": true, "argv": ["hook", "/t", "S"], "payload": ""}\n',
            b'{"id": 1, "argv": ["hook", "/t\\u0000", "S"], "payload": ""}\n',
            b'{"id": 1, "argv": ["hook", "/t", "S"], "payload": "AB=="}\n'[:-3] + b"\n",
            b"[]\n",
        ):
            with self.subTest(bad=bad[:60]):
                self.assertIsNone(relay.stub_request(bad))

    def test_the_exact_malformed_reply_alone_omits_gen_and_seq(self):
        self.assertEqual(relay.relay_reply(b'{"ok":false}\n'), {"ok": False})
        for bad in (
            b'{"ok":0}\n',
            b'{"ok":true}\n',
            b'{"gen":"%s","seq":1,"ok":1}\n' % GEN.encode(),
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(relay.relay_reply(bad))


class QueueTest(Case):
    def setUp(self):
        self.path = os.path.join(self.home, "queue")
        self.queue = relay.Queue(self.path)

    def test_stub_fixtures_get_their_expected_reply_and_only_valid_ones_are_queued(self):
        for case in cases("stub_request"):
            with self.subTest(fixture=case["file"]):
                before = self.queue.last
                self.assertEqual(
                    relay.answer(line(case), self.queue, GEN), case["expected"]["reply"]
                )
                self.assertEqual(self.queue.last, before + case["valid"])
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o700)

    def test_seq_counts_from_1_and_messages_are_relay_requests(self):
        self.assertEqual(relay.answer(request(5), self.queue, GEN), {"id": 5, "queued": True})
        relay.answer(request(6), self.queue, GEN)
        seq, message = self.queue.first(0)
        self.assertEqual(seq, 1)
        self.assertEqual(
            relay.relay_request(message),
            {"id": 5, "gen": GEN, "seq": 1, "argv": ["hook", "/tui/w", "Stop"], "payload": "e30="},
        )

    def test_a_restarted_daemon_continues_seq_and_keeps_undeleted_messages(self):
        for n in range(3):
            relay.answer(request(n), self.queue, GEN)
        self.queue.drop(1)
        self.queue.drop(3)
        again = relay.Queue(self.path)
        self.assertEqual(again.last, 3)
        self.assertEqual(again.first(0)[0], 2)
        os.unlink(os.path.join(self.path, "counter"))
        self.assertEqual(relay.Queue(self.path).last, 2)

    def test_a_failed_write_is_not_queued(self):
        with mock.patch("os.replace", side_effect=OSError(28, "No space left")):
            self.assertEqual(relay.answer(request(5), self.queue, GEN), {"id": 5, "queued": False})

    def test_a_failed_counter_write_never_reuses_the_queued_seq(self):
        replace = os.replace
        with mock.patch(
            "os.replace",
            side_effect=lambda a, b: (
                (_ for _ in ()).throw(OSError(28, "full"))
                if b.endswith("counter")
                else replace(a, b)
            ),
        ):
            self.assertEqual(relay.answer(request(5), self.queue, GEN), {"id": 5, "queued": False})
            self.assertEqual(self.queue.first(0)[0], 1)
        relay.answer(request(6), self.queue, GEN)
        self.assertEqual(self.queue.last, 2)
        self.assertEqual(json.loads(self.queue.first(0)[1])["id"], 5)

    def test_first_waits_at_most_its_limit(self):
        self.assertIsNone(self.queue.first(0))

    def test_first_lists_the_queue_once(self):
        for n in (1, 2, 3):
            relay.answer(request(n), self.queue, GEN)
        with mock.patch("os.listdir", wraps=os.listdir) as listing:
            self.assertEqual(self.queue.first(0)[0], 1)
        self.assertEqual(listing.call_count, 1)


class PumpTest(Case):
    def setUp(self):
        self.queue = relay.Queue(os.path.join(self.home, "queue"))
        for n in range(3):
            relay.answer(request(n), self.queue, GEN)

    def pump(self, *replies):
        writer = Writer()
        relay.pump(self.queue, io.BytesIO(b"".join(replies)), writer, idle=0)
        return [m["seq"] for m in writer.lines()]

    def reply(self, seq, *, ok=True):
        return json.dumps({"gen": GEN, "seq": seq, "ok": ok}).encode() + b"\n"

    def test_any_reply_drops_its_message_and_sends_the_next(self):
        sent = self.pump(self.reply(1), self.reply(2, ok=False), b'{"ok":false}\n')
        self.assertEqual(sent, [1, 2, 3])
        self.assertIsNone(self.queue.first(0))

    def test_eof_or_a_bad_or_mismatched_reply_keeps_the_message_for_replay(self):
        for replies in (
            (),
            (b"junk\n",),
            (self.reply(2),),
            (json.dumps({"gen": "f" * 32, "seq": 1, "ok": True}).encode() + b"\n",),
        ):
            with self.subTest(replies=replies):
                self.assertEqual(self.pump(*replies), [1])
                self.assertEqual(self.queue.first(0)[0], 1)
        self.assertEqual(self.pump(self.reply(1), self.reply(2), self.reply(3)), [1, 2, 3])

    def test_a_queue_file_that_is_no_relay_request_is_dropped_unsent(self):
        with open(os.path.join(self.queue.path, "1.json"), "wb") as f:
            f.write(b"not json\n")
        self.assertEqual(self.pump(self.reply(2), self.reply(3)), [2, 3])
        self.assertIsNone(self.queue.first(0))


def fake_socket(*, connect=None, accept=(), recv=(), reply=b""):
    """A socket.socket whose connect, accept and recv follow the lists given."""
    sock = mock.MagicMock()
    sock.__enter__.return_value = sock
    sock.connect.side_effect = connect
    sock.accept.side_effect = [*accept, OSError("closed")]
    sock.recv.side_effect = [*recv, b""]
    sock.written = Writer()
    sock.written.close = lambda: None
    sock.makefile.side_effect = lambda mode: io.BytesIO(reply) if mode == "rb" else sock.written
    return sock


class DaemonTest(Case):
    def setUp(self):
        self.run = os.path.join(self.home, "run")
        os.mkdir(self.run)

    def daemon(self, *sockets):
        daemon = relay.Daemon(GEN, self.run)
        with (
            mock.patch("socket.socket", side_effect=list(sockets)),
            mock.patch("threading.Thread") as thread,
        ):
            started = daemon.start()
        return daemon, started, [c.kwargs["target"] for c in thread.call_args_list]

    def test_its_sockets_are_the_modules_under_its_run_dir(self):
        self.assertEqual(
            (relay.Daemon(GEN).socket, relay.Daemon(GEN).uplink), (relay.SOCKET, relay.UPLINK)
        )
        daemon = relay.Daemon(GEN, self.run)
        self.assertEqual(
            (daemon.socket, daemon.uplink),
            (os.path.join(self.run, "relay.sock"), os.path.join(self.run, "relay-up.sock")),
        )

    def test_start_binds_nothing_while_a_daemon_answers(self):
        daemon, started, threads = self.daemon(fake_socket())
        self.assertFalse(started)
        self.assertEqual((daemon.listeners, threads), ([], []))

    def test_start_replaces_stale_sockets_and_close_shuts_both_listeners(self):
        for name in ("relay.sock", "relay-up.sock"):
            open(os.path.join(self.run, name), "w").close()
        uplink, stubs = fake_socket(), fake_socket()
        probe = fake_socket(connect=FileNotFoundError(2, "gone"))
        daemon, started, threads = self.daemon(probe, uplink, stubs)
        self.assertTrue(started)
        self.assertEqual(threads, [daemon._uplinks, daemon._stubs])
        uplink.bind.assert_called_once_with(daemon.uplink)
        stubs.bind.assert_called_once_with(daemon.socket)
        self.assertEqual(os.listdir(self.run), ["relay-queue"])
        daemon.close()
        for listener in (uplink, stubs):
            listener.shutdown.assert_called_once()
            listener.close.assert_called_once()

    def test_a_stub_connection_is_answered_and_an_uplink_is_pumped(self):
        daemon, _, _ = self.daemon(
            fake_socket(connect=OSError("none")), fake_socket(), fake_socket()
        )
        stub_conn = fake_socket(reply=request(4))
        daemon._stub(stub_conn)
        stub_conn.sendall.assert_called_once_with(b'{"id":4,"queued":true}\n')
        up = fake_socket(reply=json.dumps({"gen": GEN, "seq": 1, "ok": True}).encode() + b"\n")
        relay.answer(request(5), daemon.queue, GEN)
        daemon._uplinks(fake_socket(accept=[(up, None)]))
        self.assertEqual([m["seq"] for m in up.written.lines()], [1, 2])
        self.assertEqual(daemon.queue.first(0)[0], 2)
        with mock.patch("threading.Thread") as thread:
            daemon._stubs(fake_socket(accept=[(stub_conn, None)]))
        thread.assert_called_once_with(target=daemon._stub, args=(stub_conn,), daemon=True)


class ConnectTest(Case):
    def test_forwards_both_ways_until_either_side_ends(self):
        stdin_r, stdin_w = os.pipe()
        stdout_r, stdout_w = os.pipe()
        self.addCleanup(lambda: [os.close(fd) for fd in (stdin_r, stdout_r, stdout_w)])
        os.write(stdin_w, b"reply\n")
        os.close(stdin_w)
        conn = fake_socket(recv=[b"message\n"])
        with (
            mock.patch("socket.socket", return_value=conn),
            mock.patch("select.select", side_effect=lambda r, *_: (r, [], [])),
        ):
            self.assertEqual(relay.connect("/up", stdin_r, stdout_w), 0)
        conn.connect.assert_called_once_with("/up")
        conn.sendall.assert_called_once_with(b"reply\n")
        self.assertEqual(os.read(stdout_r, 100), b"message\n")

    def test_retries_until_its_wait_then_exits_1(self):
        conns = [fake_socket(connect=FileNotFoundError(2, "No such file")) for _ in range(3)]
        clock = iter([0, 0, 5, 11])
        err = io.StringIO()
        with (
            mock.patch("socket.socket", side_effect=conns),
            mock.patch("time.monotonic", side_effect=lambda: next(clock)),
            mock.patch("time.sleep"),
            mock.patch("sys.stderr", err),
        ):
            self.assertEqual(relay.connect("/up", wait=10), 1)
        self.assertIn("ensemble relay connect: [Errno 2] No such file", err.getvalue())


class MainTest(Case):
    def test_serve_exits_0_when_a_daemon_answers_and_connect_runs_the_connector(self):
        with mock.patch("socket.socket", return_value=fake_socket()):
            self.assertEqual(relay.main(["serve", GEN]), 0)
        with (
            mock.patch("socket.socket", return_value=fake_socket(connect=OSError("none"))),
            mock.patch("time.monotonic", side_effect=[0, 11]),
            mock.patch("sys.stderr", io.StringIO()) as err,
        ):
            self.assertEqual(relay.main(["connect"]), 1)
        self.assertIn("ensemble relay connect: none", err.getvalue())

    def test_bad_arguments_exit_2(self):
        for argv in ([], ["serve"], ["serve", "XYZ"], ["connect", "x"], ["other"]):
            with self.subTest(argv=argv), mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(relay.main(argv), 2)


class StubTest(Case):
    def conn(self, *, reply=None, error=None):
        conn = mock.MagicMock()
        conn.__enter__.return_value = conn
        conn.connect.side_effect = error

        def sent(data):
            found = json.loads(data)
            answer = {"id": found["id"], "queued": True} if reply is None else reply(found)
            out = answer if isinstance(answer, bytes) else json.dumps(answer).encode() + b"\n"
            conn.makefile.return_value.__enter__.return_value = io.BytesIO(out)

        conn.sendall.side_effect = sent
        return conn

    def run_stub(self, conn):
        err = io.StringIO()
        with mock.patch("socket.socket", return_value=conn), mock.patch("sys.stderr", err):
            code = stub.main(["hook", "/tui/w", "Stop"], io.BytesIO(b'{"a": 1}'))
        return code, err.getvalue()

    def test_the_stub_dials_the_relays_socket(self):
        self.assertEqual(stub.SOCKET, relay.SOCKET)

    def test_sends_a_stub_request_and_exits_0_on_its_queued_reply(self):
        conn = self.conn()
        self.assertEqual(self.run_stub(conn), (0, ""))
        conn.connect.assert_called_once_with("/run/ensemble/relay.sock")
        conn.settimeout.assert_called_once_with(stub.WAIT)
        sent = conn.sendall.call_args.args[0]
        self.assertIsNotNone(relay.stub_request(sent))
        self.assertEqual(base64.b64decode(json.loads(sent)["payload"]), b'{"a": 1}')

    def test_no_daemon_or_no_queued_reply_still_exits_0_with_one_line(self):
        for conn, want in (
            (self.conn(error=FileNotFoundError(2, "No such file or directory")), "No such file"),
            (self.conn(error=TimeoutError("timed out")), "timed out"),
            (self.conn(reply=lambda _: b""), "not queued"),
            (self.conn(reply=lambda r: {"id": r["id"], "queued": False}), "not queued"),
            (self.conn(reply=lambda r: {"id": r["id"] + 1, "queued": True}), "not queued"),
            (self.conn(reply=lambda r: {"id": r["id"], "queued": 1}), "not queued"),
            (self.conn(reply=lambda _: {"queued": False}), "not queued"),
        ):
            with self.subTest(want=want):
                code, err = self.run_stub(conn)
                self.assertEqual(code, 0)
                self.assertIn(want, err)
                self.assertEqual(len(err.splitlines()), 1)
