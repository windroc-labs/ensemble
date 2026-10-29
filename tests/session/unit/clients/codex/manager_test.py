"""The manager bridge preserves native protocol messages and binds before delivery."""

import io
import json
import struct
import unittest
from unittest import mock

from ensemble.session.clients.codex import manager


class ProtocolTest(unittest.TestCase):
    def test_null_config_is_a_valid_native_default(self):
        bridge = manager.ThreadBridge({"ENSEMBLE_MANAGER": "m"}, lambda _record: None)
        value = bridge.request(b'{"id":1,"method":"thread/start","params":{"config":null}}')
        self.assertEqual(
            json.loads(value)["params"]["config"]["shell_environment_policy"]["set"],
            {"ENSEMBLE_MANAGER": "m"},
        )

    def test_thread_responses_bind_native_ids_and_preserve_errors(self):
        records = []
        bridge = manager.ThreadBridge({"ENSEMBLE_MANAGER": "second"}, records.append)
        request = {
            "id": 7,
            "method": "thread/start",
            "params": {
                "config": {"shell_environment_policy": {"set": {"KEEP": "yes"}}},
            },
        }
        patched = json.loads(bridge.request(json.dumps(request).encode()))
        policy = patched["params"]["config"]["shell_environment_policy"]
        self.assertEqual(policy["set"], {"KEEP": "yes", "ENSEMBLE_MANAGER": "second"})
        self.assertIn("ENSEMBLE_AGENT_SESSION", policy["exclude"])
        response = b'{"id":7,"result":{"thread":{"id":"native","cwd":"/same"}}}'
        self.assertEqual(bridge.response(response), response)
        self.assertEqual(records, [{"session_id": "native", "cwd": "/same"}])
        bridge.request(b'{"id":8,"method":"thread/resume","params":{"threadId":"native"}}')
        error = b'{"id":8,"error":{"code":-1,"message":"failed"}}'
        self.assertEqual(bridge.response(error), error)
        self.assertEqual(len(records), 1)

    def test_two_connections_to_the_same_server_keep_their_identity(self):
        first, second = [], []
        bridges = [
            manager.ThreadBridge({"ENSEMBLE_MANAGER": name}, record.append)
            for name, record in (("first", first), ("second", second))
        ]
        for bridge in bridges:
            bridge.request(b'{"id":1,"method":"thread/start","params":{}}')
        bridges[1].response(b'{"id":1,"result":{"thread":{"id":"b","cwd":"/same"}}}')
        bridges[0].response(b'{"id":1,"result":{"thread":{"id":"a","cwd":"/same"}}}')
        self.assertEqual(first, [{"session_id": "a", "cwd": "/same"}])
        self.assertEqual(second, [{"session_id": "b", "cwd": "/same"}])

    def test_resume_and_clear_bind_before_first_turn(self):
        records = []
        bridge = manager.ThreadBridge({"ENSEMBLE_HOLDER_ID": "new-holder"}, records.append)
        bridge.request(b'{"id":1,"method":"thread/resume","params":{"threadId":"old"}}')
        bridge.response(b'{"id":1,"result":{"thread":{"id":"old","cwd":"/same"}}}')
        self.assertEqual(records[-1]["session_id"], "old")
        bridge.request(b'{"id":2,"method":"thread/start","params":{"sessionStartSource":"clear"}}')
        bridge.response(b'{"id":2,"result":{"thread":{"id":"new","cwd":"/same"}}}')
        self.assertEqual([r["session_id"] for r in records], ["old", "new"])
        turn = b'{"id":3,"method":"turn/start","params":{"threadId":"new"}}'
        self.assertEqual(bridge.request(turn), turn)

    def test_other_messages_and_server_requests_are_byte_exact(self):
        bridge = manager.ThreadBridge({}, lambda _record: self.fail("unexpected binding"))
        for value in (
            b'{"id":4,"method":"item/commandExecution/requestApproval","params":{}}',
            b'{"method":"item/agentMessage/delta","params":{"delta":"hi"}}',
            b'{"id":4,"result":null}',
            b"not JSON",
        ):
            self.assertEqual(bridge.request(value), value)
            self.assertEqual(bridge.response(value), value)

    def test_frames_preserve_large_and_masked_messages(self):
        for size in (0, 5, 126, 65536):
            for masked in (True, False):
                with self.subTest(size=size, masked=masked):
                    payload = b"x" * size
                    data = manager.frame(payload, masked=masked)
                    self.assertEqual(manager.read_frame(io.BytesIO(data)), (True, 1, payload))

    def test_fragmented_text_and_control_frames(self):
        parts = manager.frame(b"first", masked=True, final=False)
        parts += manager.frame(b"ping", masked=True, opcode=9)
        parts += manager.frame(b"second", masked=True, opcode=0)
        stream = io.BytesIO(parts)
        self.assertEqual(manager.read_frame(stream), (False, 1, b"first"))
        self.assertEqual(manager.read_frame(stream), (True, 9, b"ping"))
        self.assertEqual(manager.read_frame(stream), (True, 0, b"second"))

    def test_native_compression_offer_is_removed(self):
        request = (
            b"GET /rpc HTTP/1.1\r\nSec-WebSocket-Key: abc\r\n"
            b"x-codex-app-server-compression: zstd-stream-v1\r\n"
            b"Sec-WebSocket-Extensions: permessage-deflate\r\n\r\n"
        )
        self.assertEqual(
            manager.plain_handshake(request), b"GET /rpc HTTP/1.1\r\nSec-WebSocket-Key: abc\r\n\r\n"
        )

    def test_unauthorized_peers_are_rejected(self):
        parents = {70: 50, 50: 41, 80: 42, 42: 1, 41: 1}
        self.assertTrue(
            manager.authorized(70, 1000, 1000, launch=50, hold=41, get_parent=parents.get)
        )
        self.assertFalse(
            manager.authorized(70, 1001, 1000, launch=50, hold=41, get_parent=parents.get)
        )
        self.assertFalse(
            manager.authorized(80, 1000, 1000, launch=50, hold=41, get_parent=parents.get)
        )

    def test_unauthorized_connection_never_connects_upstream(self):
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.getsockopt.return_value = struct.pack("3i", 9999999, 1001, 1001)
        listener = mock.Mock()
        listener.accept.return_value = (client, None)
        with (
            mock.patch("sys.platform", "linux"),
            mock.patch("os.getuid", return_value=1000),
            mock.patch("socket.socket") as upstream,
            self.assertRaisesRegex(ValueError, "unauthorized"),
        ):
            manager.serve(listener, "/unused", 50, 41, ("recorder",))
        upstream.assert_not_called()
        client.sendall.assert_not_called()

    def test_darwin_credentials_decode_the_native_pid_and_uid(self):
        client = mock.Mock()
        client.getsockopt.side_effect = (
            struct.pack("IIh16I", 0, 501, 1, *([20] * 16)),
            struct.pack("i", 700),
        )
        with mock.patch("sys.platform", "darwin"):
            self.assertEqual(manager.peer(client), (700, 501))

    def test_startup_classification_does_not_intercept_noninteractive_commands(self):
        for argv in (
            ("plugin", "list"),
            ("app-server", "daemon", "start"),
            ("exec", "task"),
            ("--version",),
            ("--no-daemon",),
            ("--config", "x=1", "login"),
        ):
            with self.subTest(argv=argv):
                self.assertFalse(manager.interactive(argv))
        for argv in (
            (),
            ("resume", "native"),
            ("--model", "m", "resume", "native"),
            ("fork", "native"),
            ("--config", "x=1", "hello"),
        ):
            with self.subTest(argv=argv):
                self.assertTrue(manager.interactive(argv))
