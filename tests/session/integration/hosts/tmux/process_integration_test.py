"""TmuxProcess attachment and controller checks use real filesystem metadata."""

import os
import socket
from pathlib import Path
from unittest import mock

from ensemble.session import SessionError
from ensemble.session.engine.tmux_cli import TuiError
from ensemble.session.engine.types import PaneStatus
from ensemble.session.hosts.tmux.host import TmuxProcess, TmuxTerminal
from ensemble.utils import ProcessControl
from tests.session.tmux_host_support import LINE, PROCESS, Base


def row(name: str, display: str | None = None) -> dict:
    return {"name": name, "display": display}


class AttachmentBase(Base):
    def controller(self):
        control = ProcessControl.prepare(["tool", "a b"], self.dir)
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.bind(os.path.join(control.path, "control"))
        self.addCleanup(connection.close)
        return control


class AttachListTest(AttachmentBase):
    def test_a_launched_session_is_the_pane_it_recorded_not_its_active_one(self):
        self.tmux.process.side_effect = lambda target: (
            "4242 $3 %9 5555" if target == "w1" else PROCESS if target == "%7" else None
        )
        self.connection.connect.side_effect = ConnectionRefusedError
        for kind, cls in (("detach", TmuxProcess), ("terminal", TmuxTerminal)):
            with self.subTest(kind=kind):
                self.kind = kind
                self.tmux.kill.reset_mock()
                handle = self.host.attach("w1")
                self.assertIs(type(handle), cls)
                self.assertEqual(handle.identity, PROCESS)
                handle.close()
                self.tmux.kill.assert_called_once_with("%7")

    def test_a_session_of_either_kind_without_a_controller_is_refused_immediately(self):
        self.controlled = False
        for kind, name in (("terminal", "tui"), ("detach", "d1")):
            with self.subTest(kind=kind):
                self.kind = kind
                with self.assertRaisesRegex(SessionError, f"tmux session {name}: missing @control"):
                    self.host.attach(name)
                self.tmux.kill.assert_not_called()
                self.socket.assert_not_called()

    def test_attach_during_controller_teardown_returns_an_ended_handle(self):
        self.control.remove()
        handle = self.host.attach("w1")
        self.assertFalse(handle.alive())
        self.assertTrue(handle.wait_exit(0))
        self.connection.connect.assert_not_called()

    def test_a_regular_file_in_place_of_the_controller_socket_is_refused(self):
        self.controlled = True
        Path(self.control.path, "control").unlink()
        Path(self.control.path, "control").write_text("not a socket")
        with self.assertRaisesRegex(SessionError, "invalid @control"):
            self.host.attach("d1")
        self.socket.assert_not_called()

    def test_attach_a_live_session_else_a_driver_error(self):
        self.assertEqual(self.host.attach("d1").name, "d1")
        for name, state, message in (
            ("d1", None, "no tmux session d1"),
            ("d1", PaneStatus.UNKNOWN, "tmux session d1: tmux failed"),
            ("a b", PaneStatus.RUNNING, "no tmux session 'a b'"),
        ):
            with self.subTest(name=name, state=state):
                self.tmux.status.return_value = state
                with self.assertRaises(SessionError) as caught:
                    self.host.attach(name)
                self.assertEqual(str(caught.exception), message)
        self.tmux.status.return_value = PaneStatus.RUNNING
        self.kind, self.controlled = None, False
        for found, message in (
            (None, "no tmux session d1"),
            (TuiError("tmux: boom"), "tmux session d1: tmux: boom"),
        ):
            with self.subTest(found=found):
                self.tmux.process.side_effect = [found]
                with self.assertRaises(SessionError) as caught:
                    self.host.attach("d1")
                self.assertEqual(str(caught.exception), message)

    def test_list_every_name_whatever_its_display(self):
        self.tmux.listing.return_value = [
            row("d", "mgr"),
            row("b", None),
            row("c", "elsewhere:1.2"),
            row("a", "mgr"),
        ]
        self.assertEqual(self.host.list(), ["a", "b", "c", "d"])

    def test_list_when_tmux_fails_is_a_driver_error(self):
        self.tmux.listing.side_effect = TuiError("tmux list-sessions exited 1: boom")
        with self.assertRaisesRegex(
            SessionError, "^tmux sessions unknown: tmux list-sessions exited 1: boom$"
        ):
            self.host.list()


class ProcessTest(AttachmentBase):
    def test_close_after_controller_death_kills_only_its_captured_pane(self):
        self.active()
        process = self.host.attach("d1")
        self.connection.connect.side_effect = ConnectionRefusedError
        process.close()
        self.tmux.kill.assert_called_once_with("%7")
        self.tmux.kill.reset_mock()
        self.tmux.process.return_value = "4242 $4 %9 5555"
        process.close()
        self.tmux.kill.assert_not_called()
        self.killpg.assert_not_called()

    def test_record_is_the_stored_line_parsed_else_none(self):
        process = self.host.attach("d1")
        for stored, want in (
            (LINE.to_line(), LINE),
            (None, None),
            ("", None),
            ("{forged", None),
            ('{"v":1}', None),
            (LINE.to_line().replace('["ensemble-delegate"]', '["sh"]', 1), None),
        ):
            with self.subTest(stored=stored):
                self.tmux.option.return_value = stored
                self.assertEqual(process.record(), want)
        self.tmux.option.assert_called_with("%7", "@record")

    def test_display_is_the_stored_value_verbatim(self):
        process = self.host.attach("d1")
        for stored in (None, "mgr/right", "elsewhere:1.2"):
            with self.subTest(stored=stored):
                self.tmux.option.return_value = stored
                self.assertEqual(process.display(), stored)
        self.tmux.option.assert_called_with("%7", "@display")

    def test_alive_while_running_wait_exit_until_not(self):
        self.active({"live": False})
        process = self.host.attach("d1")
        self.tmux.status.side_effect = [
            PaneStatus.RUNNING,
            PaneStatus.RUNNING,
            0,
            None,
        ]
        self.assertTrue(process.alive())
        self.assertTrue(process.wait_exit(5))
        self.assertFalse(process.alive())
        self.tmux.status.side_effect = None
        self.tmux.status.return_value = PaneStatus.RUNNING
        process = self.host.attach("d1")
        self.assertFalse(process.wait_exit(0))
        self.tmux.status.return_value = PaneStatus.UNKNOWN
        with self.assertRaisesRegex(SessionError, "^tmux session d1: tmux failed$"):
            process.alive()

    def test_close_terminates_its_group_after_closing_its_pane_absent_is_fine(self):
        self.active({"live": False})
        process = self.host.attach("d1")
        process.close()
        self.connection.sendall.assert_called_once_with(b"signal 15\n")
        self.killpg.assert_not_called()
        self.tmux.kill.assert_called_once_with("%7")
        self.tmux.kill.side_effect = TuiError("tmux: can't find pane: %7")
        self.tmux.process.side_effect = [PROCESS, None]
        process.close()
        self.tmux.process.side_effect = None
        with self.assertRaisesRegex(SessionError, "^tmux: can't find pane: %7$"):
            process.close()

    def test_after_its_pane_ends_wait_exit_waits_for_its_group_and_kill_ends_it(self):
        self.active({"live": True}, {"live": False})
        process = self.host.attach("d1")
        self.tmux.status.return_value = None
        self.assertFalse(process.alive())
        self.assertFalse(process.wait_exit(0))
        process.kill()
        self.assertEqual(
            self.connection.sendall.call_args_list, [mock.call(b"live\n"), mock.call(b"kill\n")]
        )
        self.connection.sendall.reset_mock()
        self.assertTrue(process.wait_exit(0))
        process.kill()
        process.close()
        self.connection.sendall.assert_not_called()
        self.killpg.assert_not_called()

    def test_unobserved_exit_and_retained_dead_pane_never_signal_a_reused_group(self):
        self.controlled = True
        old = self.host.attach("d1")
        self.tmux.status.return_value = 0
        attached_dead = self.host.attach("d1")
        for process in (old, attached_dead):
            process.kill()
            process.close()
            self.assertTrue(process.wait_exit(0))
        self.socket.assert_not_called()
        self.killpg.assert_not_called()

    def test_permission_denied_retains_control_for_later_cleanup(self):
        self.active(
            {"error": "permission denied", "errno": 1},
            {"error": "permission denied", "errno": 1},
            {"live": False},
        )
        process = self.host.attach("d1")
        self.tmux.status.return_value = 0
        for operation in (lambda: process.wait_exit(0), process.kill):
            with self.assertRaisesRegex(SessionError, "permission denied"):
                operation()
        process.kill()
        self.assertTrue(process.wait_exit(0))
        self.assertEqual(self.connection.sendall.call_count, 3)

    def test_a_handle_never_acts_on_a_later_pane_of_its_name(self):
        self.active({"live": False})
        process = self.host.attach("d1")
        for later in (None, "4242 $4 %9 5555", "9999 $3 %7 4343", "4242 $3 %7 5555"):
            with self.subTest(later=later):
                self.tmux.process.return_value = later
                self.tmux.reset_mock()
                self.tmux.process.return_value = later
                self.assertEqual(
                    (process.alive(), process.record(), process.display()), (False, None, None)
                )
                process.close()
                self.assertEqual(
                    {c.args for c in self.tmux.process_status.call_args_list}, {("%7",)}
                )
                self.tmux.option.assert_not_called()
                self.tmux.kill.assert_not_called()
                self.tmux.process.assert_called_with("%7")
        self.killpg.assert_not_called()
        self.connection.connect.assert_called_once_with(
            os.path.join(os.path.realpath(self.control.path), "control")
        )

    def test_a_tmux_failure_reading_metadata_is_a_driver_error(self):
        process = self.host.attach("d1")
        self.tmux.option.side_effect = TuiError("tmux: boom")
        with self.assertRaisesRegex(SessionError, "^tmux session d1: tmux: boom$"):
            process.record()
        self.tmux.process.side_effect = TuiError("tmux: down")
        for call in (process.alive, process.display, process.close):
            with (
                self.subTest(call=call),
                self.assertRaisesRegex(SessionError, "^tmux session d1: tmux: down$"),
            ):
                call()
