"""Viewer show, hide and attach_command over a TmuxHost on recorded tmux calls."""

import contextlib
import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from unittest import mock

from ensemble.session import ConfigError, NotShowable, SessionError
from ensemble.session.engine import tmux_cli
from ensemble.session.engine.types import PaneStatus, TuiSettings, Unanswered
from ensemble.session.hosts.base import TUI_DIR, Host
from ensemble.session.hosts.tmux import TmuxHost
from ensemble.session.hosts.types import CommandView, SessionView
from ensemble.session.viewer import display as displays
from ensemble.session.viewer import hidden
from ensemble.session.viewer.viewer import VIEW_TAG, Viewer
from ensemble.utils import STALE_AGE
from tests.session.hosted_support import SID
from tests.session.tmux_fixtures import INSIDE, SOCK, environ
from tests.session.tmux_host_support import PROCESS, Base

TuiError = tmux_cli.TuiError
NO_PANE = "no anchor pane: not in tmux, no iTerm2 pane ($ITERM_SESSION_ID)"


@contextlib.contextmanager
def sleeps() -> Iterator[list[float]]:
    """time.sleep patched: this thread's calls recorded, not slept; another thread's (a harness an
    earlier test left running) sleeps as before.
    """
    caller, real, slept = threading.current_thread(), time.sleep, []

    def sleep(seconds):
        if threading.current_thread() is caller:
            slept.append(seconds)
        else:
            real(seconds)

    with mock.patch("time.sleep", side_effect=sleep):
        yield slept


def worker(name: str, sid: str | None) -> None:
    """Worker `name`'s worker state naming `sid` (None: none)."""
    folder = os.path.join(os.path.expanduser(TUI_DIR), name)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "session.json"), "w", encoding="utf-8") as f:
        json.dump({"sid": sid, "generation": "g1"}, f)


class ShowTest(Base):
    def test_show_opens_a_pane_beside_split_from_on_the_displays_side(self):
        self.enterContext(environ())
        self.runner.results.update(
            {
                "list-clients": (0, f"5 /dev/ttys004 %9 0 {SOCK}\n"),
                "pgrep": (1,),
                "split-window": (0, "%10\n"),
            }
        )
        for display in ("mgr/below/m1", "mgr/below/m1/hidden"):
            with self.subTest(display=display):
                self.runner.calls.clear()
                self.assertIsNone(self.viewer.show("w1", self.host, display=display))
                split = next(c for c in self.runner.calls if c[1] == "split-window")
                self.assertEqual(split[3], "-v")
                self.assertEqual(split[split.index("-t") + 1], "%9")
                self.assertEqual(split[-3:], ["attach", "-t", "=w1"])

    def test_show_without_a_pane_logs_why_and_returns_the_attach_command(self):
        self.enterContext(environ())
        why = f"{NO_PANE}; watch it with tmux attach -t '=w1'"
        with self.assertLogs("session", logging.WARNING) as logs:
            self.assertEqual(self.viewer.show("w1", self.host), "tmux attach -t '=w1'")
        self.assertEqual(logs.output, [f"WARNING:session:show w1: {why}"])
        self.assertEqual(self.runner.calls, [])

    def test_show_with_a_command_runs_it_instead_of_opening_a_pane(self):
        sh = ["/bin/sh", "-c"]
        self.assertIsNone(self.viewer.show("w1", self.host, command="open -a T {{session}}"))
        self.assertEqual(self.runner.calls, [[*sh, "open -a T w1"]])
        self.runner.results["/bin/sh"] = (3,)
        with self.assertLogs("session", logging.WARNING) as logs:
            self.assertEqual(
                self.viewer.show("w1", self.host, command="false"), "tmux attach -t '=w1'"
            )
        self.assertEqual(logs.output, ["WARNING:session:show w1: exit status 3"])
        del self.runner.results["/bin/sh"]
        self.assertIsNone(
            self.viewer.show("w1", self.host, display="elsewhere:1.2", command="true")
        )
        self.assertEqual(self.runner.calls, [[*sh, "open -a T w1"], [*sh, "false"], [*sh, "true"]])

    def test_show_refuses_an_absent_session_a_foreign_display_or_a_bad_setting(self):
        self.tmux.status.return_value = None
        with self.assertRaisesRegex(SessionError, "^no tmux session w1$"):
            self.viewer.show("w1", self.host)
        self.tmux.status.return_value = PaneStatus.RUNNING
        with self.assertRaisesRegex(ConfigError, r"^display 'elsewhere:1\.2': not <opener>"):
            self.viewer.show("w1", self.host, display="elsewhere:1.2")
        self.assertEqual(self.runner.calls, [])

    def test_bad_settings_are_refused_when_built(self):
        for key, values, want in (
            (
                "workers_per_column",
                (0, 10000, -1, True, 2.0, "2"),
                "workers_per_column: want an integer from 1 to 9999",
            ),
            (
                "grid_retile",
                ("All", "", "never", 1, ["all"]),
                "grid_retile: want one of all, open-close, off",
            ),
            ("status_line", (True,), "unknown key 'status_line' in [viewer]"),
        ):
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ConfigError) as caught:
                    Viewer({key: value}, tmux=self.tmux)
                self.assertEqual(str(caught.exception), want)
        for per, retile in ((1, "open-close"), (9999, "off"), (2, "all")):
            Viewer({"workers_per_column": per, "grid_retile": retile}, tmux=self.tmux)

    def test_layout_completes_the_display_once_the_pane_is_found(self):
        self.enterContext(environ(**INSIDE))
        self.runner.pane = "mgr"
        self.runner.results["list-clients"] = (0, f"5 /dev/ttys004 %9 0 {SOCK}\n")
        for display, want in (
            (None, "mgr"),
            ("/right", "mgr/right"),
            ("w1/below", "w1/below"),
            ("/below/m1", "mgr/below/m1"),
        ):
            with self.subTest(display=display):
                self.assertEqual(displays.layout(self.viewer, self.host, display), want)
        self.runner.calls.clear()
        self.assertEqual(displays.layout(self.viewer, self.host, "w1/below"), "w1/below")
        self.assertEqual(self.runner.calls, [])

    def test_layout_beside_split_from_without_an_opener_keeps_the_part_empty(self):
        self.enterContext(environ())
        self.runner.results["list-clients"] = (0, f"5 /dev/ttys004 %9 0 {SOCK}\n")
        self.assertEqual(displays.layout(self.viewer, self.host, "//m1"), "//m1")

    def test_layout_without_a_pane_or_split_from_is_refused(self):
        self.enterContext(environ())
        hint = ": run from tmux or iTerm2, or pass split_from"
        with self.assertRaises(SessionError) as caught:
            displays.layout(self.viewer, self.host, None)
        self.assertEqual(str(caught.exception), f"no pane to show the TUI beside ({NO_PANE}){hint}")
        self.tmux.status.return_value = None
        with self.assertRaises(ConfigError) as caught:
            displays.layout(self.viewer, self.host, "//m1")
        self.assertEqual(str(caught.exception), "no tmux session m1")


class HideTest(Base):
    """Over w1, whose worker state names SID: the sid of its hidden entry."""

    def setUp(self):
        super().setUp()
        worker("w1", SID)

    def options(self, **found):
        recorded = self.tmux.option.side_effect
        self.tmux.option.return_value = None
        self.tmux.option.side_effect = lambda pane, key: found.get(key[1:], recorded(pane, key))

    def test_hide_kills_the_shown_pane_and_marks_it_hidden(self):
        self.options(pane="%9", sid=SID)
        self.assertIsNone(self.viewer.hide("w1", self.host))
        self.tmux.kill.assert_called_once_with("%9")
        self.tmux.unview.assert_called_once_with("w1", (80, 24), border=True)
        self.assertTrue(hidden.read("w1", SID))

    def test_hide_returns_the_window_to_its_hosts_configured_size(self):
        self.options(pane="%9", sid=SID)
        host = TmuxHost("tmux", {"hidden_size": "100x30"}, tmux=self.tmux)
        self.assertIsNone(self.viewer.hide("w1", host))
        self.tmux.unview.assert_called_once_with("w1", (100, 30), border=True)

    def test_a_failed_hidden_size_still_hides(self):
        self.options(pane="%9", sid=SID)
        self.tmux.unview.side_effect = TuiError("tmux: boom")
        with self.assertLogs("session", logging.WARNING) as logged:
            self.assertIsNone(self.viewer.hide("w1", self.host))
        self.assertEqual(logged.output, ["WARNING:session:hide w1: hidden size: tmux: boom"])
        self.assertTrue(hidden.read("w1", SID))

    def test_no_worker_state_marks_nothing(self):
        os.unlink(os.path.join(os.path.expanduser(TUI_DIR), "w1", "session.json"))
        self.viewer.hide("w1", self.host)
        self.assertFalse(os.path.exists(os.path.join(self.ensemble, "hidden")))

    def test_show_clears_hidden(self):
        hidden.write("w1", SID, hidden=True, live=None)
        self.options(sid=SID)
        self.assertIsNone(self.viewer.show("w1", self.host, command="true"))
        self.assertFalse(hidden.read("w1", SID))

    def test_hide_leaves_an_iterm2_or_closed_pane_and_still_marks_it_hidden(self):
        for pane, process in (("w0t0p0:ABC", PROCESS), ("%9", None), (None, PROCESS)):
            with self.subTest(pane=pane, process=process):
                self.tmux.reset_mock()
                hidden.write("w1", SID, hidden=False, live=None)
                self.options(pane=pane, sid=SID)
                self.tmux.process.side_effect = lambda target, found=process: (
                    found if target == "%9" else PROCESS
                )
                self.viewer.hide("w1", self.host)
                self.tmux.kill.assert_not_called()
                self.assertTrue(hidden.read("w1", SID))

    def test_hide_kills_the_pane_of_a_sidless_session_and_marks_nothing(self):
        worker("w1", None)
        self.options(pane="%9")
        self.tmux.process.return_value = PROCESS
        self.tmux.process.side_effect = None
        self.viewer.hide("w1", self.host)
        self.tmux.kill.assert_called_once_with("%9")
        self.assertFalse(os.path.exists(os.path.join(self.ensemble, "hidden")))

    def placed(self, **found):
        """@pane and @window as tmux keeps them: an if-shell on @pane's value unsets both."""
        self.options()
        recorded = self.tmux.option.side_effect
        self.tmux.option.side_effect = lambda target, key: (
            found[key[1:]] if key[1:] in found else recorded(target, key)
        )

        def if_shell(argv):
            if argv[5] == f"#{{==:#{{@pane}},{found.get('pane')}}}":
                found.update(pane=None, window=None)
            return (0,)

        self.runner.results.update(
            {
                "list-panes": (0, "%1\n%9\n"),
                "list-windows": (0, "@1\n@2\n"),
                "if-shell": if_shell,
            }
        )
        return found

    def test_hide_if_closed_marks_only_a_gone_pane_in_a_present_window(self):
        for pane, window, want in (
            ("%8", "@2", True),
            ("%9", "@2", False),
            ("%8", "@3", False),
            (None, "@2", False),
            ("%8", None, False),
            ("w0t0p0:ABC", "@2", False),
        ):
            with self.subTest(pane=pane, window=window):
                hidden.write("w1", SID, hidden=False, live=None)
                found = self.placed(pane=pane, window=window, sid=SID)
                self.tmux.unview.reset_mock()
                self.assertIsNone(self.viewer.hide("w1", self.host, if_closed=True))
                self.assertEqual(hidden.read("w1", SID), want)
                self.assertEqual(
                    self.tmux.unview.call_args_list, [mock.call("w1", (80, 24), border=True)] * want
                )
                self.assertEqual(found["pane"], None if want else pane)
        self.tmux.kill.assert_not_called()
        self.assertNotIn(mock.call("%8"), self.tmux.process.call_args_list)

    def test_hide_if_closed_spares_a_pane_show_records_meanwhile(self):
        hidden.write("w1", SID, hidden=False, live=None)
        found = self.placed(pane="%8", window="@2", sid=SID)

        def shown_meanwhile(_argv):
            found["pane"] = "%10"
            return (0, "@1\n@2\n")

        self.runner.results["list-windows"] = shown_meanwhile
        self.viewer.hide("w1", self.host, if_closed=True)
        self.assertEqual((found["pane"], found["window"]), ("%10", "@2"))
        self.assertFalse(hidden.read("w1", SID))
        self.tmux.kill.assert_not_called()

    def test_hide_if_closed_unmarks_a_pane_show_recorded_before_the_mark(self):
        found = self.placed(pane="%8", window="@2", sid=SID)
        forget, recorded = self.runner.results["if-shell"], self.tmux.option.side_effect
        reads = iter([None, "%10"])  # forget's check of @pane, then hide's

        def show_meanwhile(argv):
            hidden.write("w1", SID, hidden=False, live=None)
            self.tmux.option.side_effect = lambda target, key: (
                next(reads) if key == "@pane" else recorded(target, key)
            )
            return forget(argv)

        self.runner.results["if-shell"] = show_meanwhile
        self.viewer.hide("w1", self.host, if_closed=True)
        self.assertEqual(found["window"], None)
        self.assertFalse(hidden.read("w1", SID))

    def test_hide_if_closed_names_a_failed_check_after_its_mark_and_stays_hidden(self):
        self.placed(pane="%8", window="@2", sid=SID)
        forget, recorded = self.runner.results["if-shell"], self.tmux.option.side_effect
        reads = iter([None, TuiError("tmux: boom")])  # forget's check of @pane, then hide's

        def read(target, key):
            if key != "@pane":
                return recorded(target, key)
            found = next(reads)
            if isinstance(found, TuiError):
                raise found
            return found

        def failing_after(argv):
            self.tmux.option.side_effect = read
            return forget(argv)

        self.runner.results["if-shell"] = failing_after
        with self.assertRaisesRegex(SessionError, "^tmux session w1: tmux: boom$"):
            self.viewer.hide("w1", self.host, if_closed=True)
        self.assertTrue(hidden.read("w1", SID))

    def test_a_hidden_mark_keeps_live_sessions_entries(self):
        then = time.time() - STALE_AGE - 60
        for name in ("w2", "w3"):
            hidden.write(name, SID, hidden=True, live=None)
            os.utime(os.path.join(self.ensemble, "hidden", f"{name}.json"), (then, then))
        self.options(pane="%9", sid=SID)
        self.runner.results["list-sessions"] = (0, "w1\nw2\n")
        self.viewer.hide("w1", self.host)
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.ensemble, "hidden"))), ["w1.json", "w2.json"]
        )

    def test_hide_forgets_the_tmux_pane_and_window_it_recorded(self):
        for pane, forgotten in (("%9", True), ("w0t0p0:ABC", False)):
            with self.subTest(pane=pane):
                self.runner.calls.clear()
                found = self.placed(pane=pane, window="@2", sid=SID)
                self.viewer.hide("w1", self.host)
                self.assertEqual(found["pane"], None if forgotten else pane)
                self.assertEqual("if-shell" in [c[1] for c in self.runner.calls], forgotten)
                self.assertTrue(hidden.read("w1", SID))

    def test_hide_if_closed_refuses_an_absent_session(self):
        self.tmux.status.return_value = None
        with self.assertRaisesRegex(SessionError, "^no tmux session w1$"):
            self.viewer.hide("w1", self.host, if_closed=True)

    def test_hide_refuses_an_absent_session(self):
        self.tmux.status.return_value = None
        with self.assertRaisesRegex(SessionError, "^no tmux session w1$"):
            self.viewer.hide("w1", self.host)
        self.tmux.kill.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(self.ensemble, "hidden")))

    def test_hide_names_a_failed_pane_kill_and_marks_nothing(self):
        self.options(pane="%9", sid=SID)
        self.tmux.kill.side_effect = TuiError("tmux: boom")
        with self.assertRaisesRegex(SessionError, "^tmux session w1: tmux: boom$"):
            self.viewer.hide("w1", self.host)
        self.assertFalse(hidden.read("w1", SID))

    def test_hide_names_a_failed_hidden_mark(self):
        self.options(pane="%9", sid=SID)
        store = os.path.join(self.ensemble, "hidden")
        os.mkdir(store, 0o500)
        self.addCleanup(os.chmod, store, 0o700)
        with self.assertRaisesRegex(
            SessionError, "^tmux session w1: hidden state: Permission denied$"
        ):
            self.viewer.hide("w1", self.host)
        self.tmux.kill.assert_called_once_with("%9")

    def test_a_failed_show_clears_hidden_while_a_pane_it_opened_still_shows_it(self):
        self.enterContext(environ(HOME=self.home))
        hidden.write("w1", SID, hidden=True, live=None)
        self.options(pane="%9", sid=SID)
        self.assertIsNotNone(self.viewer.show("w1", self.host))
        self.assertFalse(hidden.read("w1", SID))

    def test_a_failed_show_with_no_pane_of_its_keeps_hidden(self):
        self.enterContext(environ(HOME=self.home))
        hidden.write("w1", SID, hidden=True, live=None)
        self.options(pane=None, sid=SID)
        self.assertIsNotNone(self.viewer.show("w1", self.host))
        self.assertTrue(hidden.read("w1", SID))


class AttachCommandTest(Base):
    def test_attach_command_names_the_session_and_refuses_a_bad_name(self):
        self.assertEqual(self.viewer.attach_command("w1", self.host), "tmux attach -t '=w1'")
        with self.assertRaisesRegex(SessionError, "^no tmux session 'a b'$"):
            self.viewer.attach_command("a b", self.host)


class CommandViewTest(Base):
    """A Host whose attach target is a CommandView, as the Docker Host's."""

    ARGV = ("/usr/bin/env", "-u", "TMUX", "docker", "exec", "-it", "c1", "tmux", "attach")

    def setUp(self):
        super().setUp()
        self.target = CommandView(printed="docker exec -it c1 tmux attach", argv=self.ARGV)
        self.docker = mock.create_autospec(Host, instance=True)
        self.docker.name, self.docker.attachable = "docker", True
        self.docker.attach_target.return_value = self.target
        self.docker.tui_settings.return_value = TuiSettings()
        worker("d1", SID)
        self.docker.list.return_value = ["d1"]
        self.view = None
        self.tmux.option.side_effect = lambda _target, key: self.view if key == "@view" else None
        self.enterContext(environ(HOME=self.home))

    def test_show_makes_a_tagged_view_session_at_its_hosts_size_and_status_line(self):
        self.tmux.status.return_value = None
        self.runner.results["new-session"] = (0,)
        for settings, x, y, status in (
            (TuiSettings(), "80", "24", "off"),
            (TuiSettings(hidden_size=(100, 30)), "100", "30", "off"),
            (TuiSettings(status_line=True), "80", "24", "on"),
        ):
            with self.subTest(settings=settings):
                self.tmux.checked.reset_mock()
                self.docker.tui_settings.return_value = settings
                with self.assertLogs("session", logging.WARNING):
                    self.assertEqual(self.viewer.show("d1", self.docker), self.target.printed)
                self.tmux.checked.assert_any_call(
                    [
                        *("new-session", "-d", "-s", "d1", "-x", x, "-y", y, "--", *self.ARGV),
                        *(";", "set-option", "-t", "=d1:", "@view", VIEW_TAG),
                        *(";", "set-option", "-t", "=d1:", "status", status),
                    ]
                )

    def test_show_reuses_its_view_session_and_refuses_another_of_the_name(self):
        self.view = VIEW_TAG
        with self.assertLogs("session", logging.WARNING):
            self.viewer.show("d1", self.docker)
        self.view = None
        with self.assertRaisesRegex(SessionError, "^show d1: a host tmux session d1 exists$"):
            self.viewer.show("d1", self.docker)
        self.assertNotIn("new-session", [c[1] for c in self.runner.calls])

    def test_hide_returns_a_view_session_without_a_border_to_the_hidden_size(self):
        self.tmux.status.return_value = PaneStatus.RUNNING
        self.docker.tui_settings.return_value = TuiSettings(hidden_size=(100, 30))
        self.viewer.hide("d1", self.docker)
        self.tmux.unview.assert_called_once_with("d1", (100, 30), border=False)
        self.assertTrue(hidden.read("d1", SID))

    def test_a_bad_host_setting_fails_a_hide_before_it_changes_anything(self):
        self.tmux.status.return_value = PaneStatus.RUNNING
        self.docker.tui_settings.side_effect = ConfigError("status_line: want true or false")
        with self.assertRaises(ConfigError):
            self.viewer.hide("d1", self.docker)
        self.tmux.kill.assert_not_called()
        self.tmux.unview.assert_not_called()
        self.assertFalse(hidden.read("d1", SID))

    def test_hide_without_a_view_session_only_marks_it_hidden(self):
        self.tmux.status.return_value = None
        self.viewer.hide("d1", self.docker, if_closed=True)
        self.assertFalse(os.path.exists(os.path.join(self.ensemble, "hidden")))
        self.viewer.hide("d1", self.docker)
        self.assertTrue(hidden.read("d1", SID))
        self.tmux.kill.assert_not_called()

    def test_attach_command_is_the_printed_command(self):
        self.assertEqual(self.viewer.attach_command("d1", self.docker), self.target.printed)

    def test_close_kills_only_its_tagged_view_session(self):
        for view, killed in ((VIEW_TAG, True), ("docker", False), (None, False)):
            with self.subTest(view=view):
                self.tmux.kill.reset_mock()
                self.view = view
                self.viewer.close("d1", self.docker)
                self.assertEqual(self.tmux.kill.call_args_list, [mock.call("d1")] if killed else [])

    def test_close_after_its_session_is_gone_still_kills_its_view(self):
        self.docker.attach_target.side_effect = SessionError("no docker session d1")
        self.view = VIEW_TAG
        self.viewer.close("d1", self.docker)
        self.tmux.kill.assert_called_once_with("d1")

    def test_close_names_a_failed_tmux_call(self):
        self.tmux.option.side_effect = TuiError("tmux: boom")
        with self.assertRaisesRegex(SessionError, "^view d1: tmux: boom$"):
            self.viewer.close("d1", self.docker)

    def retried(self, *targets, **show):
        """show with attach_target answering `targets` in turn; its result and its sleeps."""
        self.tmux.status.return_value = None
        self.runner.results["new-session"] = (0,)
        self.docker.attach_target.side_effect = targets
        with sleeps() as self.slept, self.assertLogs("session", logging.WARNING):
            return self.viewer.show("d1", self.docker, **show)

    def test_an_unanswered_target_is_retried_then_shown(self):
        late = Unanswered("docker ps: timed out")
        self.assertEqual(self.retried(late, self.target), self.target.printed)
        self.assertEqual(self.docker.attach_target.call_count, 2)
        self.assertEqual(self.slept, [1])

    def test_three_unanswered_targets_raise_the_last(self):
        errors = [Unanswered(f"docker ps: timed out {n}") for n in range(3)]
        with self.assertRaisesRegex(Unanswered, "^docker ps: timed out 2$"):
            self.docker.attach_target.side_effect = errors
            with sleeps() as slept:
                self.viewer.show("d1", self.docker)
        self.assertEqual(self.docker.attach_target.call_count, 3)
        self.assertEqual(len(slept), 2)

    def test_other_errors_are_not_retried(self):
        for error in (SessionError("no docker session d1"), ConfigError("bad display")):
            with self.subTest(error=error):
                self.docker.attach_target.reset_mock()
                self.docker.attach_target.side_effect = error
                with sleeps() as slept, self.assertRaises(type(error)):
                    self.viewer.show("d1", self.docker)
                self.assertEqual(self.docker.attach_target.call_count, 1)
                self.assertEqual(slept, [])


class SessionViewCloseTest(Base):
    def test_close_on_a_host_whose_target_is_its_own_tmux_session_touches_no_tmux(self):
        host = mock.create_autospec(Host, instance=True)
        host.name, host.attachable = "tmux", True
        host.attach_target.return_value = SessionView("w1")
        self.viewer.close("w1", host)
        self.tmux.option.assert_not_called()
        self.tmux.kill.assert_not_called()


class NotShowableTest(Base):
    def test_a_host_without_an_attach_target_cannot_show_and_close_is_a_no_op(self):
        plain = mock.create_autospec(Host, instance=True)
        plain.name, plain.attachable = "plain", False
        plain.attach_target.return_value = None
        for call in (
            lambda: self.viewer.show("p1", plain),
            lambda: self.viewer.hide("p1", plain),
            lambda: self.viewer.attach_command("p1", plain),
        ):
            with (
                self.subTest(call=call),
                self.assertRaisesRegex(NotShowable, "^host plain cannot show$"),
            ):
                call()
        self.viewer.close("p1", plain)
        self.tmux.option.assert_not_called()
