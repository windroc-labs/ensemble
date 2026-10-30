"""The manager bridge preserves native protocol messages and binds before delivery."""

import io
import json
import os
import struct
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ensemble.session.clients.codex import manager
from tests.session.case import REPO, Case


class Executed(BaseException):
    pass


class ProtocolTest(Case):
    def test_both_routes_register_the_launcher_before_exec(self):
        for argv in (
            ("hello",),
            ("resume", "01a18728-8ee7-7123-8123-123456789abc", "--sandbox", "read-only"),
        ):
            with self.subTest(argv=argv):
                self.launch(argv, {"ENSEMBLE_MANAGER": "m"})
                calls = [call for call in self.runs if "launch" in call.args[0]]
                self.assertEqual(len(calls), 1, "every managed TUI must register before exec")
                command = calls[0].args[0]
                self.assertEqual(command[-2:], ("--client-pid", str(os.getpid())))
                event = json.loads(calls[0].kwargs["input"])
                self.assertEqual(
                    event["session_id"],
                    "01a18728-8ee7-7123-8123-123456789abc" if argv[0] == "resume" else None,
                )

    def test_prompt_separator_keeps_command_words_interactive(self):
        self.assertTrue(manager.Arguments(("--", "exec")).interactive())

    def test_prompt_and_option_values_do_not_disable_interactive_launch(self):
        for argv in (("--", "--help"), ("--model", "--help"), ("--image", "--version")):
            with self.subTest(argv=argv):
                self.assertTrue(manager.Arguments(argv).interactive())

    def test_explicit_no_daemon_is_an_interactive_launch(self):
        self.assertTrue(manager.Arguments(("--no-daemon", "hello")).interactive())
        _, called, count = self.launch(("--no-daemon", "hello"), {"ENSEMBLE_MANAGER": "m"})
        self.assertEqual(called[1].count("--no-daemon"), 1)
        self.assertEqual(count, 2)

    def test_named_and_picker_resumes_do_not_guess_a_native_sid(self):
        for argv in (("resume", "named"), ("resume", "--last"), ("--", "resume")):
            with self.subTest(argv=argv):
                self.launch(argv, {"ENSEMBLE_MANAGER": "m"})
                call = next(call for call in self.runs if "launch" in call.args[0])
                self.assertIsNone(json.loads(call.kwargs["input"])["session_id"])

    def test_remote_word_after_separator_is_a_literal_prompt(self):
        _, called, _ = self.launch(("--", "--remote=literal"), {"ENSEMBLE_MANAGER": "m"})
        self.assertEqual(called[1][-2:], ("--", "--remote=literal"))
        self.assertEqual(called[1][1], "--remote")

    def test_remote_word_in_a_value_is_not_a_remote_option(self):
        _, called, _ = self.launch(("--model", "--remote=literal"), {"ENSEMBLE_MANAGER": "m"})
        self.assertEqual(called[1][-2:], ("--model", "--remote=literal"))

    def test_cwd_word_in_a_prompt_does_not_suppress_default_cwd(self):
        _, called, _ = self.launch(("--", "--cd=/literal"), {"ENSEMBLE_MANAGER": "m"})
        self.assertEqual(called[1][3:5], ("--cd", os.getcwd()))

    def test_user_filters_survive_identity_overlay(self):
        for policy in (
            {"filters": {"SECRET*": "exclude", "PATH": "include"}},
            {"exclude": ["SECRET*"], "include_only": ["PATH"]},
            {},
        ):
            with self.subTest(policy=policy):
                bridge = manager.ThreadBridge({"ENSEMBLE_MANAGER": "m"}, lambda _record: None)
                payload = {
                    "id": 1,
                    "method": "thread/start",
                    "params": {"config": {"shell_environment_policy": policy}},
                }
                result = json.loads(bridge.request(json.dumps(payload).encode()))
                actual = result["params"]["config"]["shell_environment_policy"]
                self.assertEqual({k: v for k, v in actual.items() if k != "set"}, policy)

    def launch(self, argv, environ=None, *, context=None, server=None, expected=Executed):
        make_directory = tempfile.mkdtemp
        directory = Path(self.home, "native")
        directory.mkdir(exist_ok=True)
        native = directory / "codex"
        native.write_text("#!/bin/sh\nexit 23\n")
        native.chmod(0o755)
        settings = Path(self.home, ".codex", "config.toml")
        settings.parent.mkdir(exist_ok=True)
        settings.write_bytes(b"user_setting = true\n")
        env = {"HOME": self.home, "PATH": str(directory), **(environ or {})}
        context = context or SimpleNamespace(returncode=0, stdout='{"holder_pid":41}')
        started = server or SimpleNamespace(returncode=0, stdout='{"socketPath":"/shared"}')
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch(
                "subprocess.run",
                side_effect=[
                    context,
                    SimpleNamespace(returncode=0, stdout='{"recorded":true}'),
                    started,
                ],
            ) as run,
            mock.patch("socket.socket"),
            mock.patch(
                "tempfile.mkdtemp",
                side_effect=lambda **options: make_directory(
                    prefix=options["prefix"], dir=self.home
                ),
            ),
            mock.patch("os.chmod"),
            mock.patch("os.fork", return_value=1),
            mock.patch("os.execv", side_effect=Executed) as execute,
            self.assertRaises(expected),
        ):
            manager.main(REPO, argv)
        self.assertEqual(settings.read_bytes(), b"user_setting = true\n")
        self.runs = run.call_args_list
        return native, execute.call_args.args if execute.called else None, run.call_count

    def test_worker_and_non_manager_launches_keep_argv_without_a_proxy(self):
        arguments = ("--model", "model with spaces", "a'b$c", "--", "tail")
        for env in ({}, {"ENSEMBLE_MANAGER": "m", "ENSEMBLE_AGENT_SESSION": "1"}):
            with self.subTest(env=env):
                native, called, count = self.launch(arguments, env)
                self.assertEqual(called, (str(native), (str(native), *arguments)))
                self.assertEqual(count, 0)

    def test_manager_launch_preserves_default_and_explicit_cwd(self):
        _, called, count = self.launch(("hello",), {"ENSEMBLE_MANAGER": "m"})
        self.assertEqual(count, 3)
        args = called[1]
        self.assertIn("--cd", args)
        self.assertEqual(args[args.index("--cd") + 1], os.getcwd())
        _, called, _ = self.launch(("--cd", "/chosen", "hello"), {"ENSEMBLE_MANAGER": "m"})
        self.assertEqual(called[1].count("--cd"), 1)
        self.assertEqual(called[1][-3:], ("--cd", "/chosen", "hello"))

    def test_local_choices_keep_native_resolution_without_remote_mode(self):
        for arguments in (
            ("--add-dir", "/extra"),
            ("--worktree",),
            ("resume", "saved", "--sandbox", "read-only", "--ask-for-approval", "untrusted"),
            ("resume", "saved", "-sread-only", "-auntrusted"),
            ("--profile", "restricted"),
            ("-c", 'sandbox_workspace_write.writable_roots=["/extra"]'),
            ('--config=approval_policy="untrusted"',),
            ("--full-auto",),
            ("--dangerously-bypass-approvals-and-sandbox",),
            ("--enable", "some_feature"),
            ("--oss",),
        ):
            with self.subTest(arguments=arguments):
                native, called, count = self.launch(arguments, {"ENSEMBLE_MANAGER": "m"})
                self.assertEqual(called, (str(native), (str(native), "--no-daemon", *arguments)))
                self.assertEqual(count, 2)

    def test_native_environment_choices_do_not_get_a_remote_workspace(self):
        for name in ("CODEX_EXEC_SERVER_URL", "OPENAI_WORKLOAD_IDENTITY_CONTEXT"):
            with self.subTest(name=name):
                native, called, count = self.launch(
                    ("hello",), {"ENSEMBLE_MANAGER": "m", name: "chosen"}
                )
                self.assertEqual(called, (str(native), (str(native), "--no-daemon", "hello")))
                self.assertEqual(count, 2)

    def test_supported_options_and_literal_prompt_keep_the_shared_route(self):
        for arguments in (
            ("resume", "saved", "--last", "--all"),
            ("--model", "--sandbox", "hello"),
            ("--model=chosen", "-C/chosen", "-iimage.png", "--no-alt-screen"),
            ("--", "--sandbox"),
        ):
            with self.subTest(arguments=arguments):
                _, called, count = self.launch(arguments, {"ENSEMBLE_MANAGER": "m"})
                self.assertEqual(count, 3)
                self.assertEqual(called[1][1], "--remote")

    def test_stale_holder_passes_through_and_unavailable_server_fails_closed(self):
        native, called, count = self.launch(
            ("hello",), {"ENSEMBLE_MANAGER": "m"}, context=SimpleNamespace(returncode=0, stdout="")
        )
        self.assertEqual(called, (str(native), (str(native), "hello")))
        self.assertEqual(count, 1)
        _, called, count = self.launch(
            ("hello",),
            {"ENSEMBLE_MANAGER": "m"},
            server=SimpleNamespace(returncode=1, stdout=""),
            expected=ValueError,
        )
        self.assertIsNone(called)
        self.assertEqual(count, 3)
        _, called, count = self.launch(
            ("--remote", "unix:///elsewhere"), {"ENSEMBLE_MANAGER": "m"}, expected=ValueError
        )
        self.assertIsNone(called)
        self.assertEqual(count, 1)

    def test_executable_skips_ensemble_entries_and_fails_without_native(self):
        installed = Path(self.home, ".ensemble", "bin")
        installed.mkdir(parents=True, exist_ok=True)
        (installed / "codex").write_text("#!/bin/sh\n")
        (installed / "codex").chmod(0o755)
        native = Path(self.home, "native")
        native.mkdir()
        (native / "codex").write_text("#!/bin/sh\n")
        (native / "codex").chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(installed) + os.pathsep + str(native)}):
            self.assertEqual(
                manager.executable(REPO, str(installed / "codex")), str(native / "codex")
            )
        with (
            mock.patch.dict(os.environ, {"PATH": str(installed)}),
            self.assertRaisesRegex(ValueError, "no Codex executable"),
        ):
            manager.executable(REPO, None)

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
        self.assertEqual(
            policy["set"],
            {"KEEP": "yes", "ENSEMBLE_MANAGER": "second"},
        )
        self.assertNotIn("exclude", policy)
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

    def test_relay_binds_fragmented_response_before_delivery(self):
        events = []
        received = []
        bridge = manager.ThreadBridge(
            {}, lambda record: events.append(("binding", record["session_id"]))
        )
        bridge.request(b'{"id":1,"method":"thread/start","params":{}}')
        response = b'{"id":1,"result":{"thread":{"id":"native","cwd":"/same"}}}'
        wire = manager.frame(response[:20], masked=False, final=False)
        wire += manager.frame(b"ping", masked=False, opcode=9)
        wire += manager.frame(response[20:], masked=False, opcode=0)
        wire += manager.frame(b"", masked=False, opcode=8)
        source, target = mock.Mock(), mock.Mock()
        source.makefile.return_value = io.BytesIO(wire)

        def sent(data):
            _final, opcode, payload = manager.read_frame(io.BytesIO(data))
            events.append(("wire", opcode))
            if opcode in (0, 1):
                received.append(payload)

        target.sendall.side_effect = sent
        manager.relay(source, target, bridge.response, masked=False)
        self.assertEqual(
            events, [("wire", 9), ("binding", "native"), ("wire", 1), ("wire", 0), ("wire", 8)]
        )
        self.assertEqual(b"".join(received), response)

    def test_changed_request_keeps_the_native_frame_limit(self):
        request = b'{"id":1,"method":"thread/start","params":{}}'
        pieces = [request[at : at + 15] for at in range(0, len(request), 15)]
        wire = b"".join(
            manager.frame(
                piece, masked=True, opcode=1 if at == 0 else 0, final=at == len(pieces) - 1
            )
            for at, piece in enumerate(pieces)
        )
        wire += manager.frame(b"", masked=True, opcode=8)
        source, target = mock.Mock(), mock.Mock()
        source.makefile.return_value = io.BytesIO(wire)
        bridge = manager.ThreadBridge({"ENSEMBLE_MANAGER": "m"}, lambda _record: None)
        manager.relay(source, target, bridge.request, masked=True)
        fragments = [
            manager.read_frame(io.BytesIO(call.args[0])) for call in target.sendall.call_args_list
        ]
        data = [payload for _final, opcode, payload in fragments if opcode in (0, 1)]
        self.assertLessEqual(max(map(len, data)), 15)
        value = json.loads(b"".join(data))
        self.assertEqual(
            value["params"]["config"]["shell_environment_policy"]["set"], {"ENSEMBLE_MANAGER": "m"}
        )

    def test_recorder_rejection_or_failure_never_delivers_success(self):
        for result in (
            SimpleNamespace(returncode=1, stdout=""),
            SimpleNamespace(returncode=0, stdout="{}"),
            SimpleNamespace(returncode=0, stdout=""),
            manager.subprocess.TimeoutExpired("record", 5),
        ):
            with self.subTest(result=result):

                def record(event):
                    manager.recording(("recorder",), 70, event)

                bridge = manager.ThreadBridge({}, record)
                bridge.request(b'{"id":1,"method":"thread/start","params":{}}')
                response = b'{"id":1,"result":{"thread":{"id":"native","cwd":"/same"}}}'
                source, target = mock.Mock(), mock.Mock()
                source.makefile.return_value = io.BytesIO(manager.frame(response, masked=False))
                options = (
                    {"side_effect": result}
                    if isinstance(result, Exception)
                    else {"return_value": result}
                )
                with (
                    mock.patch("subprocess.run", **options),
                    self.assertRaises((ValueError, manager.subprocess.TimeoutExpired)),
                ):
                    manager.relay(source, target, bridge.response, masked=False)
                target.sendall.assert_not_called()

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

    def test_authenticated_server_connection_transports_and_binds(self):
        listener, client, server = mock.Mock(), mock.MagicMock(), mock.MagicMock()
        client.__enter__.return_value = client
        server.__enter__.return_value = server
        listener.accept.side_effect = (TimeoutError(), (client, None))
        client.getsockopt.return_value = struct.pack("3i", 70, 1000, 1000)
        request_header = (
            b"GET /rpc HTTP/1.1\r\nx-codex-app-server-compression: zstd-stream-v1\r\n\r\n"
        )
        response_header = b"HTTP/1.1 101 Switching Protocols\r\n\r\n"
        incoming = io.BytesIO(request_header)
        outgoing = io.BytesIO(response_header)
        client.recv.side_effect = incoming.read
        server.recv.side_effect = outgoing.read
        request = b'{"id":1,"method":"thread/start","params":{}}'
        response = b'{"id":1,"result":{"thread":{"id":"native","cwd":"/same"}}}'
        client.makefile.return_value = io.BytesIO(
            manager.frame(request, masked=True) + manager.frame(b"", masked=True, opcode=8)
        )
        server.makefile.return_value = io.BytesIO(
            manager.frame(response, masked=False) + manager.frame(b"", masked=False, opcode=8)
        )
        parents = {70: 50, 50: 41, 41: 1}

        def proc(path, *_args, **_options):
            pid = int(str(path).split("/")[2])
            return io.BytesIO(f"{pid} (tui) S {parents[pid]} 1 1".encode())

        class InlineThread:
            def __init__(self, target):
                self.target = target

            def start(self):
                self.target()

            def join(self):
                pass

        with (
            mock.patch("sys.platform", "linux"),
            mock.patch("os.getuid", return_value=1000),
            mock.patch("os.kill"),
            mock.patch("builtins.open", side_effect=proc),
            mock.patch("socket.socket", return_value=server),
            mock.patch("threading.Thread", InlineThread),
            mock.patch(
                "subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout='{"recorded":true}'),
            ) as record,
        ):
            manager.serve(listener, "/shared", 50, 41, ("recorder",))
        server.connect.assert_called_once_with("/shared")
        self.assertEqual(server.sendall.call_args_list[0].args[0], b"GET /rpc HTTP/1.1\r\n\r\n")
        self.assertEqual(
            json.loads(record.call_args.kwargs["input"]), {"session_id": "native", "cwd": "/same"}
        )
        delivered = client.sendall.call_args_list[1].args[0]
        self.assertEqual(manager.read_frame(io.BytesIO(delivered)), (True, 1, response))

    def test_protocol_limits_reject_truncated_or_oversized_input(self):
        for raw in (
            b"",
            b"\x81",
            b"\x81\x7f" + (manager.MAX_MESSAGE + 1).to_bytes(8, "big"),
            b"\xc1\x00",
        ):
            with self.subTest(raw=raw), self.assertRaises((EOFError, ValueError)):
                manager.read_frame(io.BytesIO(raw))
        connection = mock.Mock()
        connection.recv.return_value = b""
        with self.assertRaises(EOFError):
            manager.headers(connection)
        connection.recv.return_value = b"x" * 65537
        with self.assertRaisesRegex(ValueError, "too large"):
            manager.headers(connection)

    def test_startup_classification_does_not_intercept_noninteractive_commands(self):
        for argv in (
            ("plugin", "list"),
            ("app-server", "daemon", "start"),
            ("exec", "task"),
            ("--version",),
            ("--config", "x=1", "login"),
        ):
            with self.subTest(argv=argv):
                self.assertFalse(manager.Arguments(argv).interactive())
        for argv in (
            (),
            ("resume", "native"),
            ("--model", "m", "resume", "native"),
            ("fork", "native"),
            ("--config", "x=1", "hello"),
        ):
            with self.subTest(argv=argv):
                self.assertTrue(manager.Arguments(argv).interactive())
