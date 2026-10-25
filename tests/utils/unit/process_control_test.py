"""Process group ownership lasts until the controller reaps its own anchor."""

import io
import json
import os
import signal
from unittest import mock

from ensemble.utils.process_control import ROOT_ENV, STATE, TIMEOUT, ProcessControl, _Group
from tests.utils.case import Case


class GroupTest(Case):
    def test_parent_does_not_race_an_exited_anchor_to_set_its_group(self):
        with (
            mock.patch("os.fork", return_value=4242),
            mock.patch("os.setpgid", side_effect=ProcessLookupError) as setpgid,
        ):
            group = _Group.launch(["missing"], "/")
        self.addCleanup(os.close, group.channel)
        setpgid.assert_not_called()
        self.assertEqual(group.receive(), [])
        self.assertTrue(group.eof)

    def group(self):
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        self.addCleanup(os.close, write)
        os.write(write, b'{"started": true}\n{"rc": 0}\n')
        return _Group(4242, read)

    def test_unreaped_anchor_pins_descendants_then_disables_all_future_signals(self):
        group = self.group()
        with (
            mock.patch("os.killpg") as killpg,
            mock.patch("os.waitpid", return_value=(4242, 0)) as reap,
            mock.patch(
                "subprocess.run",
                return_value=mock.Mock(
                    returncode=0, stdout="4242 4242 Z\n4343 4242 S\n", stderr=""
                ),
            ) as ps,
        ):
            self.assertEqual(group.receive(), [{"started": True}, {"rc": 0}])
            self.assertTrue(group.live())
            reap.assert_not_called()
            group.kill()
            self.assertIn(mock.call(4242, signal.SIGKILL), killpg.call_args_list)
            ps.return_value.stdout = "4242 4242 Z\n"
            self.assertFalse(group.live())
            reap.assert_called_once_with(4242, 0)
            killpg.reset_mock()
            ps.return_value.stdout = "9999 4242 S\n"
            group.kill()
            self.assertFalse(group.live())
            killpg.assert_not_called()

    def test_permission_denied_retains_identity_and_never_reports_exit(self):
        group = self.group()
        with (
            mock.patch("subprocess.run", side_effect=PermissionError),
            mock.patch("os.killpg") as killpg,
            mock.patch("os.waitpid") as reap,
        ):
            for operation in (group.live, group.kill, group.live):
                with self.subTest(operation=operation), self.assertRaises(PermissionError):
                    operation()
                self.assertEqual(group.pid, 4242)
            reap.assert_not_called()
            killpg.assert_not_called()

    def test_permission_denied_signalling_does_not_forget_the_group(self):
        group = self.group()
        with (
            mock.patch("os.killpg", side_effect=[PermissionError, None]) as killpg,
            mock.patch(
                "subprocess.run",
                return_value=mock.Mock(returncode=0, stdout="4343 4242 S\n", stderr=""),
            ),
        ):
            with self.assertRaises(PermissionError):
                group.kill()
            self.assertEqual(group.pid, 4242)
            group.kill()
            self.assertEqual(killpg.call_args_list[-1], mock.call(4242, signal.SIGKILL))


class ControlTest(Case):
    def control(self):
        path = os.path.join(self.home, "controller")
        os.mkdir(path, 0o700)
        return ProcessControl(path)

    def test_the_root_is_the_callers_else_the_environments_else_tmp(self):
        caller, environment = (os.path.realpath(self.owned(name)) for name in ("caller", "env"))
        with mock.patch.dict(os.environ, {ROOT_ENV: environment}):
            for root, expected in ((caller, caller), (None, environment)):
                with self.subTest(root=root):
                    control = ProcessControl.prepare(["program"], "/", root=root)
                    self.addCleanup(control.remove)
                    self.assertEqual(os.path.dirname(control.path), expected)
                    self.assertTrue(os.path.basename(control.path).startswith("apm-group-"))
                    self.assertEqual(ProcessControl.attach(control.path).path, control.path)
        with (
            mock.patch.dict(os.environ, clear=True),
            mock.patch("tempfile.mkdtemp", return_value=control.path) as allocate,
        ):
            ProcessControl.prepare(["program"], "/")
            allocate.assert_called_once_with(prefix="apm-group-", dir="/tmp")

    def owned(self, name):
        path = os.path.join(self.home, name)
        os.mkdir(path, 0o700)
        return path

    def test_terminate_requests_term_from_the_owned_controller(self):
        control = self.control()
        self.addCleanup(control.remove)
        with mock.patch("socket.socket") as socket:
            connection = socket.return_value.__enter__.return_value
            connection.makefile.return_value.__enter__.return_value.readline.return_value = (
                b'{"live": true}\n'
            )
            control.terminate()
        connection.sendall.assert_called_once_with(b"signal 15\n")
        self.assertFalse(control.ended)

    def test_start_retries_a_bound_socket_before_the_controller_listens(self):
        control = self.control()
        self.addCleanup(control.remove)
        with open(os.path.join(control.path, "control"), "w"):
            pass
        requests = []

        def respond(command):
            requests.append(command)
            if command == b"start\n":
                with open(os.path.join(control.path, STATE), "w") as stream:
                    json.dump({"started": {"started": True}}, stream)

        with mock.patch("socket.socket") as socket, mock.patch("time.sleep"):
            connection = socket.return_value.__enter__.return_value
            connection.connect.side_effect = [ConnectionRefusedError, None, None]
            connection.sendall.side_effect = respond
            connection.makefile.return_value.__enter__.return_value.readline.return_value = (
                b'{"live": true}\n'
            )
            control.wait_started()
        self.assertEqual(connection.connect.call_count, 3)
        self.assertEqual(requests, [b"start\n", b"started\n"])
        self.assertFalse(control.ended)

    def test_a_controller_that_never_listens_still_times_out(self):
        control = self.control()
        self.addCleanup(control.remove)
        with open(os.path.join(control.path, "control"), "w"):
            pass
        with (
            mock.patch("socket.socket") as socket,
            mock.patch("time.monotonic", side_effect=[0, 0, 1]),
            mock.patch("time.sleep"),
        ):
            connection = socket.return_value.__enter__.return_value
            connection.connect.side_effect = ConnectionRefusedError
            with self.assertRaisesRegex(TimeoutError, "process controller did not start"):
                control.wait_started(timeout=1)
        connection.connect.assert_called_once()
        self.assertFalse(control.ended)

    def test_connection_refusal_after_start_is_not_a_startup_retry(self):
        control = self.control()
        self.addCleanup(control.remove)
        with open(os.path.join(control.path, "control"), "w"):
            pass
        with open(os.path.join(control.path, STATE), "w") as stream:
            json.dump({"started": {"started": True}}, stream)
        with mock.patch("socket.socket") as socket, mock.patch("time.sleep") as sleep:
            connection = socket.return_value.__enter__.return_value
            connection.connect.side_effect = ConnectionRefusedError
            with self.assertRaises(ConnectionRefusedError):
                control.wait_started()
        connection.connect.assert_called_once()
        sleep.assert_not_called()

    def test_a_signal_forwarding_socket_error_does_not_end_the_pane(self):
        control = self.control()
        self.addCleanup(control.remove)
        read, write = os.pipe()
        os.write(write, b"0\n")
        handlers = {}
        with (
            mock.patch("os.pipe", return_value=(read, write)),
            mock.patch("os.fork", return_value=4242),
            mock.patch("os.waitpid", return_value=(0, 0)) as wait,
            mock.patch("socket.socket") as socket,
            mock.patch(
                "signal.signal", side_effect=lambda sig, handler: handlers.update({sig: handler})
            ),
            mock.patch(
                "signal.pthread_sigmask",
                side_effect=lambda *_: handlers[signal.SIGTERM](signal.SIGTERM, None),
            ),
        ):
            socket.return_value.__enter__.return_value.connect.side_effect = PermissionError(
                "denied"
            )
            self.assertEqual(control.run(), 0)
        wait.assert_called_once_with(4242, os.WNOHANG)
        socket.return_value.__enter__.return_value.connect.assert_called_once()

    def test_losing_the_pane_during_startup_cancels_before_any_launch_request(self):
        control = self.control()
        self.addCleanup(control.remove)
        with mock.patch("os.getppid", return_value=1), mock.patch("select.select") as poll:
            self.assertFalse(control._await_start(mock.Mock(), parent=4242))
        self.assertTrue(control.cancelled)
        self.assertFalse(control.requested)
        poll.assert_not_called()

    def test_losing_the_pane_sends_term_once_and_keeps_ownership_through_slow_cleanup(self):
        control = self.control()
        self.addCleanup(control.remove)
        control.accepted = True
        clock = [0.0]
        group = mock.Mock(returncode=None, eof=False)
        group.live.side_effect = lambda: clock[0] < 7

        def receive():
            if clock[0] == 0:
                return [{"started": True}]
            if clock[0] >= 6:
                group.returncode = 0
            return []

        def poll(_read, _write, _error, timeout):
            clock[0] += timeout
            return [], [], []

        group.receive.side_effect = receive
        exited = mock.Mock()
        with (
            mock.patch("time.monotonic", side_effect=lambda: clock[0]),
            mock.patch("select.select", side_effect=poll),
            mock.patch("os.getppid", return_value=1),
            mock.patch("signal.signal"),
            mock.patch("signal.pthread_sigmask"),
        ):
            self.assertEqual(
                control._serve(mock.Mock(), group, exited, detached=True, parent=4242), 0
            )
        group.send.assert_called_once_with(signal.SIGTERM)
        group.kill.assert_not_called()
        exited.assert_called_once_with(0)
        self.assertGreaterEqual(clock[0], 7)

    def test_background_membership_probes_back_off_while_requests_remain_responsive(self):
        control = self.control()
        self.addCleanup(control.remove)
        control.accepted = True
        clock, probes = [0.0], []
        group = mock.Mock(returncode=0, eof=False)
        group.receive.return_value = [{"started": True}]
        group.live.side_effect = lambda: probes.append(clock[0]) or clock[0] < 5

        def poll(_read, _write, _error, timeout):
            clock[0] += timeout
            return [], [], []

        with (
            mock.patch("time.monotonic", side_effect=lambda: clock[0]),
            mock.patch("select.select", side_effect=poll),
            mock.patch("signal.signal"),
            mock.patch("signal.pthread_sigmask"),
        ):
            self.assertEqual(control._serve(mock.Mock(), group, None), 0)
        self.assertGreater(len(probes), 3)
        self.assertLessEqual(len(probes), 11)
        self.assertGreaterEqual(probes[-1] - probes[-2], 1)

    def serve(self, control, group, live_until=0.0):
        clock = [0.0]
        group.live.side_effect = lambda: clock[0] < live_until

        def poll(_read, _write, _error, timeout):
            clock[0] += timeout
            return [], [], []

        exited = mock.Mock()
        with (
            mock.patch("time.monotonic", side_effect=lambda: clock[0]),
            mock.patch("select.select", side_effect=poll),
            mock.patch("signal.signal"),
            mock.patch("signal.pthread_sigmask"),
        ):
            rc = control._serve(mock.Mock(), group, exited)
        return rc, clock[0], exited

    def test_an_unaccepted_ended_group_retires_after_the_limit(self):
        control = self.control()
        self.addCleanup(control.remove)
        group = mock.Mock(returncode=3, eof=False)
        group.receive.return_value = [{"started": True}]
        rc, ended, exited = self.serve(control, group, live_until=2)
        self.assertEqual(rc, 3)
        self.assertGreaterEqual(ended, 2 + TIMEOUT)
        self.assertLess(ended, 2 + TIMEOUT + 1)
        exited.assert_called_once_with(3)
        with open(os.path.join(control.path, STATE)) as stream:
            self.assertEqual(
                json.load(stream), {"started": {"started": True}, "exited": 3, "done": 3}
            )
        self.assertEqual(os.listdir(control.path), [STATE])

    def test_a_missing_directory_retires_an_ended_group_at_once_and_never_a_live_one(self):
        control = self.control()
        control.remove()
        group = mock.Mock(returncode=None, eof=False)

        def receive():
            if group.receive.call_count == 1:
                return [{"started": True}]
            if not group.live():
                group.returncode = 5
            return []

        group.receive.side_effect = receive
        rc, ended, exited = self.serve(control, group, live_until=4)
        self.assertEqual(rc, 5)
        self.assertGreaterEqual(ended, 4)
        self.assertLess(ended, 5)
        exited.assert_called_once_with(5)
        self.assertFalse(os.path.exists(control.path))

    def test_a_launch_that_fails_before_a_start_retry_is_answered_waits_for_its_acceptance(self):
        for accepting in (b"started\n", b"kill\n", b"signal 15\n"):
            with self.subTest(accepting=accepting):
                control = self.control()
                self.addCleanup(control.remove)
                group = mock.Mock(returncode=None, eof=False)
                group.live.return_value = False

                def receive(group=group):
                    if group.returncode is not None:
                        return []
                    group.returncode = 1
                    return [{"error": "No such file or directory", "errno": 2}]

                group.receive.side_effect = receive
                clients = [Connection(b"start\n"), Connection(accepting)]
                listener = mock.Mock()
                listener.accept.side_effect = [(client, None) for client in clients]
                with (
                    mock.patch("select.select", return_value=([listener], [], [])),
                    mock.patch("signal.signal"),
                    mock.patch("signal.pthread_sigmask"),
                ):
                    self.assertEqual(control._serve(listener, group, None), 1)
                self.assertEqual(
                    [json.loads(client.answer) for client in clients],
                    [{"live": True}, {"live": False}],
                )
                with open(os.path.join(control.path, STATE)) as stream:
                    self.assertEqual(json.load(stream)["done"], 1)
                control.remove()

    def test_a_query_during_directory_removal_reports_ended(self):
        control = self.control()
        self.addCleanup(control.remove)
        socket_path = os.path.join(control.path, "control")
        with open(socket_path, "w"):
            pass

        def removed(_path):
            os.unlink(socket_path)
            raise FileNotFoundError("controller retired its socket")

        with mock.patch("socket.socket") as socket:
            connection = socket.return_value.__enter__.return_value
            connection.connect.side_effect = removed
            self.assertFalse(control.live())
            self.assertTrue(control.ended)
            self.assertTrue(os.path.isdir(control.path))
            control.kill()
            connection.connect.assert_called_once()

    def test_a_denied_socket_stat_does_not_retire_the_generation(self):
        control = self.control()
        self.addCleanup(control.remove)
        original = os.stat

        def denied(path, *args, **kwargs):
            if path == os.path.join(control.path, "control"):
                raise PermissionError("cannot inspect controller socket")
            return original(path, *args, **kwargs)

        with mock.patch("socket.socket") as socket, mock.patch("os.stat", side_effect=denied):
            connection = socket.return_value.__enter__.return_value
            connection.connect.side_effect = ConnectionRefusedError("cannot connect")
            with self.assertRaises(PermissionError):
                control.live()
        self.assertFalse(control.ended)

    def test_a_completed_generation_never_contacts_or_signals_a_reused_group(self):
        control = self.control()
        self.addCleanup(control.remove)
        with open(os.path.join(control.path, STATE), "w") as stream:
            json.dump({"done": 0}, stream)
        with mock.patch("socket.socket") as socket, mock.patch("os.killpg") as killpg:
            control.terminate()
            control.kill()
            self.assertFalse(control.live())
            socket.assert_not_called()
            killpg.assert_not_called()


class Connection:
    """One accepted control connection: its command, then the answer the controller sends."""

    def __init__(self, command: bytes):
        self.command, self.answer = command, b""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def settimeout(self, _seconds):
        return None

    def makefile(self, _mode):
        return io.BytesIO(self.command)

    def sendall(self, data: bytes):
        self.answer += data
