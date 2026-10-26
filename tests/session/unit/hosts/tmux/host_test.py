"""TmuxHost launch/display boundaries and process-close ordering over recorded calls.
Attachment trust uses real metadata in integration/hosts/tmux/process_integration_test.py.
"""

import json
import os
import re
import signal
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ensemble.session import ConfigError, Host, Launch, SessionError
from ensemble.session.engine import tmux_cli
from ensemble.session.engine.tmux_cli import (
    TmuxCli,
)
from ensemble.session.engine.types import (
    ClientContext,
    Key,
    KeyPress,
    PaneStatus,
    Paste,
    Permission,
)
from ensemble.session.hosts.base import SIGNALS, InteractiveProcess
from ensemble.session.hosts.tmux import TmuxHost
from ensemble.session.hosts.tmux.host import TmuxProcess, TmuxTerminal
from ensemble.session.hosts.types import SessionView
from ensemble.utils import ProcessControl, load_class
from tests.session.case import Case
from tests.session.hosted_support import SID
from tests.session.tmux_fixtures import FakeTmux
from tests.session.tmux_host_support import LINE, PROCESS, Base

TuiError = tmux_cli.TuiError
NO_PANE = "no anchor pane: not in tmux, no iTerm2 pane ($ITERM_SESSION_ID)"
OTHER = "1f8fad5b-d9cb-469f-a165-70867728950e"


class ClassTest(Case):
    def test_capacity_namespace_uses_the_actual_server_socket_not_the_alias_settings(self):
        tmux = mock.create_autospec(TmuxCli, instance=True)
        tmux.checked.return_value = SimpleNamespace(stdout="/fake/server.sock\n")
        first = TmuxHost("first", {"status_line": False}, tmux=tmux)
        alias = TmuxHost("alias", {"status_line": True}, tmux=tmux)
        self.assertEqual(first._capacity_namespace(), alias._capacity_namespace())
        self.assertEqual(first._capacity_namespace(), "tmux:/fake/server.sock")
        tmux.checked.assert_called_with(["display-message", "-p", "#{socket_path}"])
        tmux.checked.return_value = SimpleNamespace(stdout="/fake/other.sock\n")
        self.assertNotEqual(first._capacity_namespace(), "tmux:/fake/server.sock")

    def test_unknown_capacity_namespace_fails_closed(self):
        tmux = mock.create_autospec(TmuxCli, instance=True)
        host = TmuxHost("first", tmux=tmux)
        tmux.checked.side_effect = TuiError("offline")
        with self.assertRaisesRegex(SessionError, "inventory.*offline"):
            host._capacity_namespace()
        tmux.checked.side_effect = None
        for value in ("", "\n", "invalid\npath"):
            tmux.checked.return_value = SimpleNamespace(stdout=value)
            with self.subTest(value=value), self.assertRaises(SessionError):
                host._capacity_namespace()

    def test_an_attachable_host_loaded_by_its_config_name(self):
        cls = load_class("ensemble.session.hosts.tmux:TmuxHost", Host)
        self.assertIs(cls, TmuxHost)
        self.assertTrue(cls.attachable)
        host = TmuxHost("tmux", {"status_line": True})
        self.assertEqual((host.name, host.settings), ("tmux", {"status_line": True}))
        self.assertFalse(host._isolated)
        self.assertTrue(TmuxHost("tmux", {"isolated": True})._isolated)
        self.assertIsInstance(host.tmux, TmuxCli)
        self.assertEqual(
            (f"{host.tmux.runner.__module__}.{host.tmux.runner.__name__}", host.tmux.sleep),
            ("subprocess.run", time.sleep),
        )

    def test_client_context_is_the_loopback_and_the_isolated_permission_mode(self):
        self.assertEqual(
            TmuxHost("tmux", {})._client_context(), ClientContext("127.0.0.1", Permission.AUTO)
        )
        self.assertEqual(
            TmuxHost("tmux", {"isolated": True})._client_context(),
            ClientContext("127.0.0.1", Permission.YOLO),
        )


class DetachTest(Base):
    def test_tmux_failure_removes_the_launch_without_killing_an_existing_name(self):
        paths = []

        def refuse(*_args, **kw):
            paths.append(kw["control"])
            raise TuiError("session d1 exists")

        self.tmux.detach.side_effect = refuse
        with self.assertRaises(SessionError) as caught:
            self.detach()
        self.assertEqual(str(caught.exception), "detach d1: session d1 exists")
        self.assertEqual(len(paths), 1)
        self.assertFalse(os.path.exists(paths[0]))
        self.tmux.kill.assert_not_called()

    def test_startup_timeout_cancels_launch_and_kills_only_the_captured_pane(self):
        paths = []

        def pending(*_args, **kw):
            paths.append(kw["control"])
            return PROCESS

        self.tmux.detach.side_effect = pending
        with (
            mock.patch("time.monotonic", side_effect=[0, 11]),
            self.assertRaisesRegex(SessionError, "process controller did not start"),
        ):
            self.detach()
        self.assertFalse(os.path.exists(paths[0]))
        self.tmux.kill.assert_called_once_with("%7")

    def test_a_cancellation_socket_error_is_a_driver_error(self):
        def refuse(*_args, **kw):
            control = ProcessControl(kw["control"])
            self.addCleanup(control.remove)
            Path(control.path, "control").touch()
            raise TuiError("cannot create pane")

        self.tmux.detach.side_effect = refuse
        self.connection.connect.side_effect = TimeoutError("cancel timed out")
        with self.assertRaisesRegex(
            SessionError, "cannot create pane; cancel controller: cancel timed out"
        ):
            self.detach()
        self.tmux.kill.assert_not_called()

    def test_a_cancellation_deadline_is_a_driver_error(self):
        def refuse(*_args, **kw):
            control = ProcessControl(kw["control"])
            self.addCleanup(control.remove)
            Path(control.path, "control").touch()
            raise TuiError("cannot create pane")

        self.tmux.detach.side_effect = refuse
        with (
            mock.patch("time.monotonic", side_effect=[0, 11]),
            self.assertRaisesRegex(
                SessionError, "cancel controller: process controller did not cancel"
            ),
        ):
            self.detach()
        self.tmux.kill.assert_not_called()

    def test_a_startup_cleanup_tmux_error_is_a_driver_error(self):
        self.tmux.detach.side_effect = lambda *_args, **_kw: PROCESS
        self.tmux.process.side_effect = TuiError("cannot inspect pane")
        with (
            mock.patch("time.monotonic", side_effect=[0, 11]),
            self.assertRaisesRegex(
                SessionError, "process controller did not start; close pane: cannot inspect pane"
            ),
        ):
            self.detach()
        self.tmux.kill.assert_not_called()

    def test_failed_cancellation_still_closes_only_the_owned_pane(self):
        def pending(*_args, **kw):
            control = ProcessControl(kw["control"])
            self.addCleanup(control.remove)
            Path(control.path, "control").touch()
            return PROCESS

        self.tmux.detach.side_effect = pending
        self.connection.connect.side_effect = PermissionError("controller denied")
        with (
            mock.patch("time.monotonic", side_effect=[0, 11]),
            self.assertRaisesRegex(SessionError, "cancel controller: controller denied"),
        ):
            self.detach()
        self.tmux.kill.assert_called_once_with("%7")

    def detach(self, **kw):
        return self.host.detach(
            "d1", Launch((self.tool, "a b"), cwd=self.dir, env={"PATH": "/bin"}), **kw
        )

    def test_forwards_the_launch_the_display_and_the_record_line(self):
        events = os.path.join(self.dir, "events")
        line = replace(LINE, sid=SID)
        process = self.detach(display="mgr/below", events=events, record=line)
        self.tmux.detach.assert_called_once_with(
            "d1",
            Launch(tuple(self.control.command()), cwd=self.dir, env={"PATH": "/bin"}),
            display="mgr/below",
            events=events,
            record=line.to_line(),
            sid=SID,
            control=self.control.path,
        )
        self.assertEqual((process.name, process.alive()), ("d1", True))
        self.assertEqual(
            json.loads(Path(self.control.path, "launch.json").read_text()),
            {
                "argv": [self.tool, "a b"],
                "cwd": self.dir,
                "env": {"PATH": "/bin"},
            },
        )
        self.tmux.status.assert_called_with("%7")

    def test_only_an_explicit_launch_mask_is_applied_to_the_target(self):
        self.host.detach(
            "d1",
            Launch((self.tool,), cwd=self.dir, blocked_signals=SIGNALS),
        )
        self.assertEqual(
            json.loads(Path(self.control.path, "launch.json").read_text())["blocked_signals"],
            list(SIGNALS),
        )

    def test_any_display_is_stored_as_given(self):
        for display in (None, "", "/right", "elsewhere:1.2", "mgr///hidden"):
            with self.subTest(display=display):
                self.tmux.detach.reset_mock()
                self.detach(display=display)
                kw = self.tmux.detach.call_args.kwargs
                self.assertEqual((kw["display"], kw["record"]), (display, None))

    def test_a_display_tmux_could_not_list_or_a_bad_events_file_is_a_config_error(self):
        for kw, message in (
            ({"display": "mgr\tx"}, r"^display 'mgr\\tx': "),
            ({"display": "mgr\n"}, "^display "),
            ({"display": "mgr;"}, "^display "),
            ({"events": self.dir}, f"^events file {re.escape(self.dir)}: "),
        ):
            with self.subTest(**kw), self.assertRaisesRegex(ConfigError, message):
                self.detach(**kw)
        self.tmux.detach.assert_not_called()

    def test_events_creation_failure_is_a_driver_error_before_launch(self):
        events, real_open = os.path.join(self.dir, "events"), os.open

        def refused(path, *args, **kw):
            if path == events:
                raise PermissionError(13, "Permission denied")
            return real_open(path, *args, **kw)

        with (
            mock.patch("os.open", side_effect=refused),
            self.assertRaisesRegex(SessionError, f"events file {events}: Permission denied"),
        ):
            self.detach(events=events)
        self.tmux.detach.assert_not_called()
        self.socket.assert_not_called()


class TerminalTest(Base):
    def test_a_terminal_that_never_starts_closes_only_its_captured_pane(self):
        paths = []

        def pending(*_args, **kw):
            paths.append(kw["tags"]["@control"])
            return PROCESS

        self.tmux.start.side_effect = pending
        with (
            mock.patch("time.monotonic", side_effect=[0, 11]),
            self.assertRaisesRegex(SessionError, "process controller did not start"),
        ):
            self.host.terminal("t1", Launch((self.tool,), cwd=self.dir))
        self.assertFalse(os.path.exists(paths[0]))
        self.tmux.kill.assert_called_once_with("%7")

    def test_only_a_key_following_a_paste_waits_for_paste_delivery(self):
        self.active(*[{"live": True}] * 4)
        terminal = TmuxTerminal("t1", PROCESS, tmux=self.tmux, control=self.control)
        order = mock.Mock()
        order.attach_mock(self.tmux.paste, "paste")
        order.attach_mock(self.tmux.keys, "keys")
        with mock.patch("time.sleep") as sleep:
            order.attach_mock(sleep, "sleep")
            terminal.send(Key("Escape"))
            terminal.send(Paste("a b"))
            terminal.send(Key("Enter"))
            terminal.send(Key("Escape"))
        self.assertEqual(
            order.mock_calls,
            [
                mock.call.keys("%7", "Escape"),
                mock.call.paste("%7", "a b"),
                mock.call.sleep(self.tmux.PAUSE),
                mock.call.keys("%7", "Enter"),
                mock.call.keys("%7", "Escape"),
            ],
        )

    def test_press_translates_every_key_then_sends_them_in_one_tmux_call(self):
        self.active()
        for notation, names in (
            (
                ("Escape", "3", "PageUp", "PageDown", " ", ";", "C-M-S-Up", "-", "BTab", "C-c"),
                ("Escape", "3", "PPage", "NPage", "Space", "\\;", "C-M-S-Up", "-", "BTab", "C-c"),
            ),
            # ctrl reaches letters, space and the C0 punctuation
            (
                ("C- ", "C-/", "C-@", "C-[", "C-\\", "C-]", "C-^", "C-_", "C-?", "C-M-[", "C-A"),
                (
                    "C-Space",
                    "C-/",
                    "C-@",
                    "C-[",
                    "C-\\",
                    "C-]",
                    "C-^",
                    "C-_",
                    "C-?",
                    "C-M-[",
                    "C-A",
                ),
            ),
        ):
            with self.subTest(notation=notation):
                self.tmux.press.reset_mock()
                terminal = TmuxTerminal("t1", PROCESS, tmux=self.tmux, control=self.control)
                terminal.press(tuple(map(KeyPress.parse, notation)))
                self.tmux.press.assert_called_once_with("%7", *names)

    def test_a_key_it_cannot_translate_sends_none_of_the_keys(self):
        self.active()
        terminal = TmuxTerminal("t1", PROCESS, tmux=self.tmux, control=self.control)
        for bad in ("Up", *map(KeyPress.parse, ("C-1", "C-2", "C--", "C-;", "C-{", "S-a"))):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                terminal.press((KeyPress("Up"), bad))
        self.tmux.press.assert_not_called()
        terminal.press((KeyPress.parse("C-c"), KeyPress.parse("S-Up"), KeyPress.parse("M-1")))
        self.tmux.press.assert_called_once_with("%7", "C-c", "S-Up", "M-1")

    def test_a_press_following_a_paste_waits_for_paste_delivery(self):
        self.active()
        terminal = TmuxTerminal("t1", PROCESS, tmux=self.tmux, control=self.control)
        with mock.patch("time.sleep") as sleep:
            terminal.send(Paste("a b"))
            terminal.press((KeyPress("Enter"),))
        sleep.assert_called_once_with(self.tmux.PAUSE)

    def test_terminal_launch_has_a_controller_identity_and_keeps_its_metadata(self):
        events = os.path.join(self.dir, "events")
        launch = Launch((self.tool, "a b"), cwd=self.dir, env={"PATH": "/bin", "TERM": "exact"})
        terminal = self.host.terminal("t1", launch, events=events, display="mgr/below", record=LINE)
        self.assertIsInstance(terminal, InteractiveProcess)
        self.assertEqual(terminal.identity, PROCESS)
        self.tmux.start.assert_called_once_with(
            "t1",
            Launch(tuple(self.control.command()), cwd=self.dir, env=launch.env),
            events=events,
            status_line=False,
            tags={
                "@kind": "terminal",
                "@control": self.control.path,
                "@display": "mgr/below",
                "@record": LINE.to_line(),
            },
        )
        terminal.send(Paste("a b"))
        self.tmux.paste.assert_called_once_with("%7", "a b")
        self.assertEqual(
            json.loads(Path(self.control.path, "launch.json").read_text())["env"], dict(launch.env)
        )

    def test_a_terminal_is_the_pane_its_start_made_not_the_sessions_active_one(self):
        self.tmux.process.side_effect = lambda target: (
            PROCESS if target == "%7" else "4242 $3 %9 5555"
        )
        terminal = self.host.terminal("t1", Launch((self.tool,), cwd=self.dir))
        self.assertEqual(terminal.identity, PROCESS)
        terminal.send(Paste("a b"))
        self.tmux.paste.assert_called_once_with("%7", "a b")

    def test_terminal_with_no_controller_is_refused_when_attaching(self):
        self.kind, self.controlled = "terminal", False
        with self.assertRaisesRegex(SessionError, "missing @control"):
            self.host.attach("t1")


class AttachTargetTest(Base):
    def test_attach_target_is_the_session_itself_and_refuses_a_bad_name(self):
        self.assertTrue(TmuxHost.attachable)
        self.assertEqual(self.host.attach_target("w1"), SessionView("w1"))
        with self.assertRaisesRegex(SessionError, "^no tmux session 'a b'$"):
            self.host.attach_target("a b")


class AttachTest(Base):
    def setUp(self):
        super().setUp()
        self.tmux.process.side_effect = lambda target: (
            "4242 $3 %9 5555" if target == "w1" else PROCESS if target == "%7" else None
        )

    def test_a_launched_session_without_a_valid_process_option_is_refused(self):
        for value, error in (
            (None, "^tmux session w1: missing @process; close sessions before upgrading$"),
            ("4242 $3 %9", r"^tmux session w1: bad @process '4242 \$3 %9'$"),
        ):
            with self.subTest(value=value):
                self.recorded = value
                with self.assertRaisesRegex(SessionError, error):
                    self.host.attach("w1")

    def test_a_foreign_session_is_its_active_pane(self):
        self.kind, self.controlled = None, False
        handle = self.host.attach("w1")
        self.assertIs(type(handle), TmuxProcess)
        self.assertEqual(handle.identity, "4242 $3 %9 5555")


class MarkerTest(Base):
    def test_a_launch_writes_no_hidden_entry_and_no_opener(self):
        launch = Launch((self.tool,), cwd=self.dir, env={"PATH": "/bin"})
        metadata = {"sid": SID, "client": "claude", "generation": "g1"}
        self.host.terminal("t1", launch, display="mgr///hidden", metadata=metadata)
        self.host.detach("d1", launch, display="mgr///hidden")
        self.assertNotIn("@opener", self.tmux.start.call_args.kwargs["tags"])
        self.assertNotIn("opener", self.tmux.detach.call_args.kwargs)
        self.assertFalse(os.path.exists(os.path.join(self.ensemble, "hidden")))


class ScreenTest(Base):
    def test_screen_reads_the_launched_panes_last_lines(self):
        self.tmux.read.return_value = "text"
        self.assertEqual(self.host.attach("w1").screen(5), "text")
        self.tmux.read.assert_called_once_with("%7", 5)

    def test_screen_of_a_session_it_did_not_launch_reads_its_active_pane(self):
        self.kind, self.controlled = None, False
        self.tmux.process.return_value = "4242 $3 %9 5555"
        self.tmux.read.return_value = "text"
        self.assertEqual(self.host.attach("w1").screen(), "text")
        self.tmux.read.assert_called_once_with("%9", None)

    def test_screen_of_a_dead_pane_still_its_own_reads_its_last_text(self):
        process = self.host.attach("w1")
        self.tmux.status.return_value = 3
        self.tmux.read.return_value = "last words"
        self.assertFalse(process.alive())
        self.assertEqual(process.screen(), "last words")

    def test_screen_of_a_pane_no_longer_its_own_is_refused_before_reading(self):
        process = self.host.attach("w1")
        self.tmux.process.return_value = "4242 $3 %7 9999"
        with self.assertRaisesRegex(SessionError, "^tmux session w1 is gone$"):
            process.screen()
        self.tmux.read.assert_not_called()

    def test_screen_names_a_failed_read(self):
        self.tmux.read.side_effect = TuiError("tmux: boom")
        with self.assertRaisesRegex(SessionError, "^tmux session w1: tmux: boom$"):
            self.host.attach("w1").screen()

    def test_screen_refuses_a_bad_launch_record_before_reading(self):
        self.recorded = "4242 $3 %x 4343"
        with self.assertRaisesRegex(
            SessionError, "^tmux session w1: bad @process '4242 \\$3 %x 4343'$"
        ):
            self.host.attach("w1").screen()
        self.tmux.read.assert_not_called()

    def test_screen_names_a_failed_launch_record_read_before_reading(self):
        self.tmux.option.side_effect = TuiError("tmux: lost server")
        with self.assertRaisesRegex(SessionError, "^tmux session w1: tmux: lost server$"):
            self.host.attach("w1").screen()
        self.tmux.read.assert_not_called()

    def test_screen_refuses_a_bad_name_before_tmux(self):
        with self.assertRaisesRegex(SessionError, "^no tmux session 'a b'$"):
            self.host.attach("a b").screen()
        self.tmux.option.assert_not_called()
        self.tmux.read.assert_not_called()


class PublishTest(Base):
    def test_publish_sets_the_sessions_record_option_while_its_pane_is_its_own(self):
        process = TmuxProcess("w1", PROCESS, tmux=self.tmux, control=self.control)
        process.publish(LINE)
        self.tmux.set_option.assert_called_once_with("%7", "@record", LINE.to_line())
        self.assertEqual(process.token, self.control.path)
        self.tmux.process.return_value = "9 $1 %7 9"
        with self.assertRaisesRegex(SessionError, "^tmux session w1 is gone$"):
            process.publish(LINE)
        with self.assertRaises(ConfigError):
            process.publish(LINE.to_line())
        self.assertEqual(self.tmux.set_option.call_count, 1)


class CloseTest(Base):
    def test_a_failed_pane_kill_still_terminates_the_owned_process_group(self):
        self.active({"live": True}, {"live": False})
        process = TmuxProcess("w1", PROCESS, tmux=self.tmux, control=self.control)
        self.tmux.kill.side_effect = TuiError("tmux: boom")
        with self.assertRaisesRegex(SessionError, "^tmux: boom$"):
            process.close()
        self.assertEqual(
            self.connection.sendall.call_args_list,
            [mock.call(b"signal 15\n"), mock.call(b"live\n")],
        )

    def test_close_during_controller_retirement_never_sends_a_numeric_group_signal(self):
        self.active()
        Path(self.control.path, "control").touch(mode=0o600)
        process = TmuxProcess("w1", PROCESS, tmux=self.tmux, control=self.control)
        for failure in (ConnectionRefusedError, ConnectionResetError):
            with self.subTest(failure=failure):
                self.connection.connect.side_effect = failure
                process.close()
        self.killpg.assert_not_called()
        self.connection.sendall.assert_not_called()
        self.assertFalse(self.control.ended)

    def test_a_denied_termination_preserves_the_controller_for_later_cleanup(self):
        self.active()
        process = TmuxProcess("w1", PROCESS, tmux=self.tmux, control=self.control)
        self.connection.connect.side_effect = PermissionError("termination denied")
        with self.assertRaisesRegex(SessionError, "process group w1: termination denied"):
            process.close()
        self.assertFalse(self.control.ended)
        self.killpg.assert_not_called()

    def test_a_failed_pane_kill_and_a_denied_termination_both_reach_the_error(self):
        self.active()
        process = TmuxProcess("w1", PROCESS, tmux=self.tmux, control=self.control)
        self.tmux.kill.side_effect = TuiError("tmux: boom")
        self.connection.connect.side_effect = PermissionError("termination denied")
        with self.assertRaises(SessionError) as caught:
            process.close()
        self.assertEqual(str(caught.exception), "tmux: boom; process group w1: termination denied")

    def test_a_pane_whose_process_exited_during_close_is_gone(self):
        # tmux prints #{pane_pid} empty once the pane's process exited
        for answers, kill in (
            (["4242 $3 %7 "], []),
            (["4242 $3 %7 4343", "4242 $3 %7 "], [["tmux", "kill-pane", "-t", "%7"]]),
        ):
            with self.subTest(answers=answers):
                self.active({"live": True}, {"live": False})
                printed = iter(answers)
                fake = FakeTmux(
                    results={
                        "display-message": lambda _argv, printed=printed: (0, next(printed) + "\n"),
                        "kill-pane": (1, "", "can't find pane: %7\n"),
                    }
                )
                process = TmuxProcess("w1", PROCESS, tmux=TmuxCli(fake), control=self.control)
                process.close()
                self.assertEqual([c for c in fake.calls if c[1] == "kill-pane"], kill)

    def test_attach_wraps_a_failed_kind_read_as_a_driver_error(self):
        self.tmux.option.side_effect = TuiError("tmux: lost server")
        with self.assertRaises(SessionError) as caught:
            self.host.attach("t1")
        self.assertEqual(str(caught.exception), "tmux session t1: tmux: lost server")

    def test_close_removes_the_pane_then_allows_term_cleanup_without_hard_kill(self):
        self.active({"live": True}, {"live": True}, {"live": False})
        process = TmuxProcess("w1", PROCESS, tmux=self.tmux, control=self.control)
        order = mock.Mock()
        order.attach_mock(self.tmux.kill, "pane")
        order.attach_mock(self.connection.sendall, "request")
        with mock.patch("time.sleep") as sleep:
            process.close()
        self.assertEqual(
            order.mock_calls,
            [
                mock.call.pane("%7"),
                mock.call.request(b"signal 15\n"),
                mock.call.request(b"live\n"),
                mock.call.request(b"live\n"),
            ],
        )
        sleep.assert_called_once_with(0.1)

    def test_close_forces_survivors_only_after_the_grace_period(self):
        self.active({"live": True})
        process = TmuxProcess("w1", PROCESS, tmux=self.tmux, control=self.control)
        clock, requests, killed = [0.0], [], [False]

        def request(command):
            requests.append((command, clock[0]))
            if command == b"kill\n":
                killed[0] = True

        self.connection.sendall.side_effect = request
        stream = self.connection.makefile.return_value.__enter__.return_value
        stream.readline.side_effect = lambda: json.dumps({"live": not killed[0]}).encode() + b"\n"
        with (
            mock.patch("time.monotonic", side_effect=lambda: clock[0]),
            mock.patch(
                "time.sleep", side_effect=lambda _seconds: clock.__setitem__(0, clock[0] + 1)
            ),
        ):
            process.close()
        self.assertEqual(requests[0], (b"signal 15\n", 0))
        self.assertEqual(
            [request for request in requests if request[0] == b"kill\n"], [(b"kill\n", 5)]
        )

    def test_close_reports_a_group_that_survives_the_forced_kill_without_forgetting_it(self):
        self.active({"live": True})
        process = TmuxProcess("w1", PROCESS, tmux=self.tmux, control=self.control)
        stream = self.connection.makefile.return_value.__enter__.return_value
        stream.readline.side_effect = None
        clock = [0.0]
        with (
            mock.patch("time.monotonic", side_effect=lambda: clock[0]),
            mock.patch(
                "time.sleep", side_effect=lambda _seconds: clock.__setitem__(0, clock[0] + 1)
            ),
            self.assertRaisesRegex(SessionError, "process group w1: did not exit"),
        ):
            process.close()
        self.assertEqual(clock[0], 10)
        self.assertFalse(self.control.ended)
        self.assertEqual(self.connection.sendall.call_args_list[0], mock.call(b"signal 15\n"))


class ReturncodeTest(Base):
    def test_the_captured_exit_status_survives_pane_removal_immediately_afterward(self):
        process = TmuxProcess("w1", PROCESS, tmux=self.tmux, control=self.control)
        self.tmux.process.side_effect = [PROCESS, TuiError("bad tmux process '4242  %7 4343'")]
        self.tmux.status.return_value = 7
        self.assertEqual(process.returncode(), 7)

    def test_absence_unknown_and_running_status_are_distinct(self):
        process = TmuxProcess("w1", PROCESS, tmux=self.tmux, control=self.control)
        for identity, status, expected in (
            (None, PaneStatus.RUNNING, -signal.SIGHUP),
            (PROCESS, None, -signal.SIGHUP),
            (PROCESS, PaneStatus.UNKNOWN, None),
            (PROCESS, PaneStatus.RUNNING, None),
            (PROCESS, 0, 0),
        ):
            with self.subTest(identity=identity, status=status):
                self.tmux.process.return_value = identity
                self.tmux.status.return_value = status
                self.assertEqual(process.returncode(), expected)
        self.tmux.process.side_effect = TuiError("tmux unavailable")
        self.assertIsNone(process.returncode())


class ListTest(Base):
    def test_a_failed_listing_names_why_in_list_and_rows(self):
        self.tmux.listing.side_effect = TuiError("tmux list-sessions exited 1: lost server")
        for verb in (self.host.list, self.host.rows):
            with (
                self.subTest(verb=verb.__name__),
                self.assertRaisesRegex(
                    SessionError,
                    r"\Atmux sessions unknown: tmux list-sessions exited 1: lost server\Z",
                ),
            ):
                verb()
