"""hosts/docker/stub.py with ENSEMBLE_GUARD_HOOK_SOCKET set: one hook request to the guard's
socket, acknowledged by its delivery id or resent unchanged, over fake connections.
"""

import io
import json
import os
from unittest import mock

from ensemble.session.hosts.docker import stub
from tests.session.case import Case

SOCKET = "/run/ensemble/guard/0123/hook"


class GuardStubTest(Case):
    def setUp(self):
        super().setUp()
        self.sent, self.replies = [], []
        patcher = mock.patch.dict(os.environ, {"ENSEMBLE_GUARD_HOOK_SOCKET": SOCKET})
        patcher.start()
        self.addCleanup(patcher.stop)

    def connection(self, *_):
        conn = mock.MagicMock()
        conn.__enter__.return_value = conn

        def sent(data):
            found = json.loads(data)
            self.sent.append(found)
            reply = self.replies.pop(0) if self.replies else "ack"
            if isinstance(reply, BaseException):
                raise reply
            out = (
                json.dumps({"ok": True, "delivery_id": found["delivery_id"]}).encode() + b"\n"
                if reply == "ack"
                else reply
            )
            conn.makefile.return_value.__enter__.return_value = io.BytesIO(out)

        conn.sendall.side_effect = sent
        return conn

    def run_stub(self, argv=("hook", "/tui/w", "Stop"), payload=b'{"a": 1}', clock=None):
        err = io.StringIO()
        with (
            mock.patch("socket.socket", side_effect=self.connection),
            mock.patch("sys.stderr", err),
            mock.patch("time.sleep"),
            mock.patch("time.monotonic", side_effect=clock or (lambda: 0.0)),
        ):
            code = stub.main(list(argv), io.BytesIO(payload))
        return code, err.getvalue()

    def test_an_acknowledged_hook_is_sent_once(self):
        self.assertEqual(self.run_stub(), (0, ""))
        self.assertEqual(len(self.sent), 1)
        request = self.sent[0]
        self.assertEqual(set(request), {"label", "payload", "delivery_id"})
        self.assertEqual((request["label"], request["payload"]), ("Stop", {"a": 1}))
        self.assertRegex(request["delivery_id"], r"\A[0-9a-f]{32}\Z")

    def test_a_missing_or_mismatched_ack_resends_the_same_request(self):
        self.replies = [
            OSError("refused"),
            b"",
            b'{"ok": true, "delivery_id": "' + b"f" * 32 + b'"}\n',
            b"{not json\n",
        ]
        self.assertEqual(self.run_stub(), (0, ""))
        self.assertEqual(len(self.sent), 5)
        self.assertEqual(len({json.dumps(found, sort_keys=True) for found in self.sent}), 1)

    def test_no_ack_within_wait_gives_up_with_one_line_and_exits_0(self):
        self.replies = [b""] * 100
        times = iter([0.0, 0.0, 4.0, 8.0, 11.0, 11.0])
        code, err = self.run_stub(clock=lambda: next(times))
        self.assertEqual(code, 0)
        self.assertEqual(err, "ensemble hook: guard did not acknowledge\n")
        self.assertEqual(len(self.sent), 3)

    def test_bad_args_or_payload_exit_0_unsent(self):
        for argv, payload in (
            (("hook", "/tui/w"), b"{}"),
            (("other", "/tui/w", "Stop"), b"{}"),
            (("hook", "/tui/w", "Stop"), b"[1]"),
            (("hook", "/tui/w", "Stop"), b"{bad"),
        ):
            with self.subTest(argv=argv, payload=payload):
                code, err = self.run_stub(argv, payload)
                self.assertEqual(code, 0)
                self.assertEqual(len(err.splitlines()), 1)
        self.assertEqual(self.sent, [])

    def test_unset_variable_prints_one_line_and_exits_0_unconnected(self):
        del os.environ["ENSEMBLE_GUARD_HOOK_SOCKET"]
        err = io.StringIO()
        with mock.patch("socket.socket") as made, mock.patch("sys.stderr", err):
            self.assertEqual(stub.main(["hook", "/tui/w", "Stop"], io.BytesIO(b"{}")), 0)
        made.assert_not_called()
        self.assertEqual(err.getvalue(), "ensemble hook: guard hook socket unavailable\n")
