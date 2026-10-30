"""Viewer over a TmuxHost on recorded tmux calls."""

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
from ensemble.session.engine.types import TuiSettings, Unanswered
from ensemble.session.hosts.base import TUI_DIR, Host
from ensemble.session.hosts.types import CommandView
from ensemble.session.viewer import display as displays
from ensemble.session.viewer import hidden
from ensemble.session.viewer import viewer as viewers
from ensemble.session.viewer.viewer import Viewer
from ensemble.utils import STALE_AGE
from tests.session.hosted_support import SID
from tests.session.tmux_fixtures import (
    CLIENTS,
    INSIDE,
    clients,
    environ,
    record,
    split,
    split_window,
    worker_rows,
)
from tests.session.tmux_host_support import Base as HostBase

TuiError = tmux_cli.TuiError
PRINTED = "/fake/attach w1"
NO_PANE = "no anchor pane: not in tmux"


@contextlib.contextmanager
def sleeps() -> Iterator[list[float]]:
    """time.sleep patched for this thread only: an earlier test's harness may still sleep."""
    caller, real, slept = threading.current_thread(), time.sleep, []

    def sleep(seconds):
        if threading.current_thread() is caller:
            slept.append(seconds)
        else:
            real(seconds)

    with mock.patch("time.sleep", side_effect=sleep):
        yield slept


def worker(name: str, sid: str | None, display: str | None = None) -> None:
    """Worker `name`'s worker state naming `sid` (None: none) and `display`."""
    folder = os.path.join(os.path.expanduser(TUI_DIR), name)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "session.json"), "w", encoding="utf-8") as f:
        json.dump({"sid": sid, "generation": "g1", "display": display}, f)


def closed(pane: str, window: str) -> list[str]:
    """Placement.close's one call for worker pane `pane` of `window`."""
    return [
        *("tmux", "set-hook", "-u", "-w", "-t", window, f"after-kill-pane[{pane[1:]}]"),
        *(";", "kill-pane", "-t", pane),
    ]


class Base(HostBase):
    """The tmux Host's attach for w: "/fake/attach w"."""

    def setUp(self):
        super().setUp()

        def target(name):
            if not isinstance(name, str) or not tmux_cli.NAME.fullmatch(name):
                raise SessionError(f"no tmux session {name!r}")
            return CommandView(f"/fake/attach {name}", ("/fake/attach", name))

        self.enterContext(mock.patch.object(self.host, "attach_target", side_effect=target))


class ShowTest(Base):
    def test_show_opens_a_pane_beside_split_from_on_the_displays_side(self):
        self.enterContext(environ(**INSIDE))
        self.runner.pane = "mgr"
        self.runner.results.update({"list-clients": (0, "5\t%9\n"), "split-window": split()})
        for display in ("mgr/below/m1", "mgr/below/m1/hidden"):
            with self.subTest(display=display):
                self.runner.calls.clear()
                self.assertIsNone(self.viewer.show("w1", self.host, display=display))
                command = next(c for c in self.runner.calls if c[1] == "split-window")
                self.assertEqual(command[3], "-v")
                self.assertEqual(command[command.index("-t") + 1], "%9")
                self.assertEqual(command[command.index("--") + 1 :], ["/fake/attach", "w1"])

    def test_show_outside_tmux_prints_the_host_command(self):
        """AC4: outside tmux, show returns CommandView.printed, no tmux."""
        self.enterContext(environ())
        for display in (None, "mgr", "mgr/below/m1"):
            with self.subTest(display=display):
                self.assertEqual(
                    self.viewer.show("w1", self.host, display=display), "/fake/attach w1"
                )
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.viewer.attach_command("w1", self.host), "/fake/attach w1")

    def test_show_refuses_a_name_a_tmux_session_holds(self):
        """AC17: a worker named like a live host tmux session is not shown."""
        self.enterContext(environ(**INSIDE))
        self.tmux.has_session.return_value = True
        with self.assertRaisesRegex(SessionError, "^show w1: a host tmux session w1 exists$"):
            self.viewer.show("w1", self.host, display="mgr")
        self.tmux.has_session.assert_called_once_with("w1")
        self.assertEqual(self.runner.calls, [])

    def test_show_passes_the_status_line_as_label_and_the_workers_sid(self):
        """AC12: status_line labels the pane; the close hook needs the worker's sid."""
        self.enterContext(environ(HOME=self.home, **INSIDE))
        self.runner.pane = "mgr"
        self.runner.results.update({"list-clients": (0, "5\t%3\n"), "split-window": split()})
        for sid, line in ((SID, False), (SID, True), (None, False)):
            with self.subTest(sid=sid, status_line=line):
                worker("w1", sid)
                self.runner.calls.clear()
                self.host.settings = {"status_line": line}
                self.assertIsNone(self.viewer.show("w1", self.host, display="mgr/right"))
                self.assertIn(split_window("-h", "%3", ("/fake/attach", "w1")), self.runner.calls)
                tagged = [c for c in self.runner.calls if c[1] == "set-option" and "-p" in c]
                found = record(
                    "mgr", "%20", name="w1", sid=sid, label=line, hook=bool(sid), printed=PRINTED
                )
                self.assertEqual(tagged, found[-1:])

    def test_show_with_a_command_runs_it_instead_of_opening_a_pane(self):
        sh = ["/bin/sh", "-c"]
        self.assertIsNone(self.viewer.show("w1", self.host, command="open -a T {{session}}"))
        self.assertEqual(self.runner.calls, [[*sh, "open -a T w1"]])
        self.runner.results["/bin/sh"] = (3,)
        with self.assertLogs("session", logging.WARNING) as logs:
            self.assertEqual(self.viewer.show("w1", self.host, command="false"), "/fake/attach w1")
        self.assertEqual(logs.output, ["WARNING:session:show w1: exit status 3"])
        del self.runner.results["/bin/sh"]
        self.assertIsNone(
            self.viewer.show("w1", self.host, display="elsewhere:1.2", command="true")
        )
        self.assertEqual(self.runner.calls, [[*sh, "open -a T w1"], [*sh, "false"], [*sh, "true"]])

    def test_show_refuses_an_absent_session_a_foreign_display_or_a_bad_setting(self):
        with self.assertRaisesRegex(ConfigError, r"^display 'elsewhere:1\.2': not <opener>"):
            self.enterContext(environ(**INSIDE))
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
        self.runner.results["list-clients"] = (0, "5\t%9\n")
        self.tmux.has_session.side_effect = lambda name: name == "m1"
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
        self.runner.results["list-clients"] = (0, "5\t%9\n")
        self.tmux.has_session.return_value = True
        self.assertEqual(displays.layout(self.viewer, self.host, "//m1"), "//m1")

    def test_layout_without_a_pane_or_split_from_is_refused(self):
        self.enterContext(environ())
        hint = ": run from tmux, or pass split_from"
        with self.assertRaises(SessionError) as caught:
            displays.layout(self.viewer, self.host, None)
        self.assertEqual(str(caught.exception), f"no pane to show the TUI beside ({NO_PANE}){hint}")
        with self.assertRaises(ConfigError) as caught:
            displays.layout(self.viewer, self.host, "//m1")
        self.assertEqual(str(caught.exception), "no tmux session m1")


class OpenerChainTest(Base):
    def test_chain_follows_display_openers_to_none_a_bad_state_a_repeat_or_eight_links(self):
        """AC9 (D5): each worker's display opener from opener's on."""
        worker("a", SID, "b/below")
        worker("b", SID, "mgr")
        worker("c", SID, "d")
        worker("d", SID, "c//m1")
        worker("e", SID, "x y")
        worker("f", SID, "f")
        worker("g", SID, None)
        for i in range(20):
            worker(f"w{i}", SID, f"w{i + 1}/right")
        folder = os.path.join(os.path.expanduser(TUI_DIR), "h")
        os.makedirs(folder)
        with open(os.path.join(folder, "session.json"), "w", encoding="utf-8") as f:
            f.write("{")
        for opener, chain in (
            ("a", ("b", "mgr")),
            ("b", ("mgr",)),
            ("c", ("d",)),
            ("e", ()),
            ("f", ()),
            ("g", ()),
            ("h", ()),
            ("mgr", ()),
            (None, ()),
            ("w0", tuple(f"w{i}" for i in range(1, 9))),
        ):
            with self.subTest(opener=opener):
                self.assertEqual(viewers._chain(opener), chain)

    def test_a_show_beside_a_hidden_opener_lands_where_its_opener_is(self):
        """AC9 (D5): w1 with hidden opener a opens beside mgr's pane; its opener stays a."""
        self.enterContext(environ(HOME=self.home, **INSIDE))
        worker("a", SID, "mgr")
        worker("w1", SID, "a")
        self.runner.pane = "mgr"
        self.runner.results.update(
            {
                "list-clients": lambda argv: (
                    clients((5, "%9")) if argv[3] == "=mgr" else (1, "", "can't find session: a\n")
                ),
                "split-window": split(),
            }
        )
        self.assertIsNone(self.viewer.show("w1", self.host))
        self.assertIn(split_window("-h", "%3", ("/fake/attach", "w1")), self.runner.calls)
        self.assertIn(["tmux", "list-clients", "-t", "=a", "-F", CLIENTS], self.runner.calls)
        tagged = next(c for c in self.runner.calls if c[1] == "set-option" and "-p" in c)
        self.assertEqual(tagged, record("a", "%20", name="w1", sid=SID, printed=PRINTED)[-1])


class HideTest(Base):
    """Over w1, whose worker state names SID: the sid of its hidden entry."""

    def setUp(self):
        super().setUp()
        worker("w1", SID)

    def panes(self, *rows):
        self.runner.results["list-panes"] = worker_rows(*rows)

    def listings(self, *results):
        results = iter(results)
        self.runner.results["list-panes"] = lambda _argv: next(results)

    def kills(self):
        return [c for c in self.runner.calls if "kill-pane" in c]

    def test_hide_kills_only_its_worker_panes_and_marks_hidden(self):
        """AC5: hide closes w1's worker panes alone and marks it hidden; the worker runs on."""
        self.panes(("%5", "w1", "mgr", "@1"), ("%6", "w2", "mgr", "@1"), ("%8", "w1", "", "@2"))
        self.assertIsNone(self.viewer.hide("w1", self.host))
        self.assertEqual(self.kills(), [closed("%5", "@1"), closed("%8", "@2")])
        self.assertTrue(hidden.read("w1", SID))
        self.assertNotIn("resize-window", [c[1] for c in self.runner.calls])

    def test_hide_without_a_worker_pane_still_marks_it_hidden(self):
        self.panes(("%6", "w2", "mgr"))
        self.viewer.hide("w1", self.host)
        self.assertEqual(self.kills(), [])
        self.assertTrue(hidden.read("w1", SID))

    def test_no_worker_state_marks_nothing(self):
        os.unlink(os.path.join(os.path.expanduser(TUI_DIR), "w1", "session.json"))
        self.viewer.hide("w1", self.host)
        self.assertFalse(os.path.exists(os.path.join(self.ensemble, "hidden")))

    def test_show_clears_hidden(self):
        hidden.write("w1", SID, hidden=True, live=None)
        self.assertIsNone(self.viewer.show("w1", self.host, command="true"))
        self.assertFalse(hidden.read("w1", SID))

    def test_hide_kills_the_pane_of_a_sidless_session_and_marks_nothing(self):
        worker("w1", None)
        self.panes(("%9", "w1", "mgr"))
        self.viewer.hide("w1", self.host)
        self.assertEqual(self.kills(), [closed("%9", "@1")])
        self.assertFalse(os.path.exists(os.path.join(self.ensemble, "hidden")))

    def test_hide_if_closed_with_a_live_pane_marks_nothing(self):
        """AC7: a close hook whose worker still has a pane (another show) changes nothing."""
        hidden.write("w1", SID, hidden=False, live=None)
        self.panes(("%10", "w1", "mgr"))
        self.assertIsNone(self.viewer.hide("w1", self.host, if_closed=True))
        self.assertFalse(hidden.read("w1", SID))
        self.assertEqual(self.kills(), [])
        self.assertNotIn("set-hook", [c[1] for c in self.runner.calls])

    def test_hide_if_closed_marks_a_worker_left_without_a_pane(self):
        """AC6: the hook of a pane killed by hand marks its worker hidden, killing nothing."""
        hidden.write("w1", SID, hidden=False, live=None)
        self.panes(("%6", "w2", "mgr"))
        self.viewer.hide("w1", self.host, if_closed=True)
        self.assertTrue(hidden.read("w1", SID))
        self.assertEqual(self.kills(), [])

    def test_hide_if_closed_rechecks_a_racing_show(self):
        """AC7: a show whose pane lands between the hook's check and its mark leaves w1 shown."""
        hidden.write("w1", SID, hidden=False, live=None)
        seen = []

        def shown_meanwhile():
            seen.append(hidden.read("w1", SID))  # the recheck runs after the hidden mark
            return worker_rows(("%10", "w1", "mgr"))

        results = iter([lambda: worker_rows(), shown_meanwhile])
        self.runner.results["list-panes"] = lambda _argv: next(results)()
        self.viewer.hide("w1", self.host, if_closed=True)
        self.assertEqual(seen, [True])
        self.assertFalse(hidden.read("w1", SID))
        self.assertEqual(self.kills(), [])

    def test_hide_if_closed_names_a_failed_check_after_its_mark_and_stays_hidden(self):
        self.listings(worker_rows(), (1, "", "boom\n"))
        with self.assertRaisesRegex(SessionError, "^tmux session w1: tmux: boom$"):
            self.viewer.hide("w1", self.host, if_closed=True)
        self.assertTrue(hidden.read("w1", SID))

    def test_a_failed_worker_pane_read_marks_nothing(self):
        for if_closed in (False, True):
            with self.subTest(if_closed=if_closed):
                self.listings((1, "", "boom\n"))
                with self.assertRaisesRegex(SessionError, "^tmux session w1: tmux: boom$"):
                    self.viewer.hide("w1", self.host, if_closed=if_closed)
                self.assertFalse(hidden.read("w1", SID))

    def test_a_hidden_mark_keeps_live_sessions_entries(self):
        # live: the Host's sessions; a tmux session's name keeps no entry
        then = time.time() - STALE_AGE - 60
        for name in ("w2", "w3"):
            hidden.write(name, SID, hidden=True, live=None)
            os.utime(os.path.join(self.ensemble, "hidden", f"{name}.json"), (then, then))
        self.enterContext(mock.patch.object(self.host, "list", return_value=["w1", "w2"]))
        self.runner.results["list-sessions"] = (0, "w3\n")
        self.viewer.hide("w1", self.host)
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.ensemble, "hidden"))), ["w1.json", "w2.json"]
        )

    def test_hide_names_a_failed_pane_kill_and_marks_nothing(self):
        self.panes(("%9", "w1", "mgr"))
        self.runner.results["set-hook"] = (1, "", "boom\n")
        with self.assertRaisesRegex(SessionError, "^tmux session w1: tmux: boom$"):
            self.viewer.hide("w1", self.host)
        self.assertFalse(hidden.read("w1", SID))

    def test_hide_names_a_failed_hidden_mark(self):
        self.panes(("%9", "w1", "mgr"))
        store = os.path.join(self.ensemble, "hidden")
        os.mkdir(store, 0o500)
        self.addCleanup(os.chmod, store, 0o700)
        with self.assertRaisesRegex(
            SessionError, "^tmux session w1: hidden state: Permission denied$"
        ):
            self.viewer.hide("w1", self.host)
        self.assertEqual(self.kills(), [closed("%9", "@1")])

    def failed_show(self):
        self.enterContext(environ(HOME=self.home, **INSIDE))
        self.runner.pane = "mgr"
        self.runner.results.update(
            {"list-clients": (0, "5\t%3\n"), "split-window": (1, "", "boom\n")}
        )
        hidden.write("w1", SID, hidden=True, live=None)
        with self.assertLogs("session", logging.WARNING) as logs:
            self.assertEqual(self.viewer.show("w1", self.host, display="mgr"), "/fake/attach w1")
        self.assertEqual(logs.output, ["WARNING:session:show w1: tmux: boom"])

    def test_a_failed_show_clears_hidden_while_a_worker_pane_still_shows_it(self):
        self.panes(("%9", "w1", "mgr"))
        self.failed_show()
        self.assertFalse(hidden.read("w1", SID))

    def test_a_failed_show_with_no_worker_pane_keeps_hidden(self):
        for rows in (worker_rows(("%9", "w2", "mgr")), (1, "", "boom\n")):
            with self.subTest(rows=rows):
                self.runner.results["list-panes"] = rows
                self.failed_show()
                self.assertTrue(hidden.read("w1", SID))


class CloseTest(Base):
    def setUp(self):
        super().setUp()
        worker("w1", SID)

    def test_close_kills_every_worker_pane_of_the_name_and_leaves_hidden(self):
        hidden.write("w1", SID, hidden=False, live=None)
        self.runner.results["list-panes"] = worker_rows(
            ("%5", "w1", "mgr", "@1"), ("%6", "w2", "mgr", "@1"), ("%8", "w1", "", "@2")
        )
        self.assertIsNone(self.viewer.close("w1", self.host))
        kills = [c for c in self.runner.calls if "kill-pane" in c]
        self.assertEqual(kills, [closed("%5", "@1"), closed("%8", "@2")])
        self.assertFalse(hidden.read("w1", SID))

    def test_close_on_a_host_that_cannot_attach_calls_no_tmux(self):
        plain = mock.create_autospec(Host, instance=True)
        plain.name, plain.attachable = "plain", False
        self.viewer.close("w1", plain)
        self.assertEqual(self.runner.calls, [])

    def test_close_names_a_failed_tmux_call(self):
        for results in ({"list-panes": (1, "", "boom\n")}, {"set-hook": (1, "", "boom\n")}):
            with self.subTest(results=results):
                self.runner.results["list-panes"] = worker_rows(("%9", "w1", "mgr"))
                self.runner.results.update(results)
                with self.assertRaisesRegex(SessionError, "^view w1: tmux: boom$"):
                    self.viewer.close("w1", self.host)
                self.runner.results.pop("set-hook", None)


class AttachCommandTest(Base):
    def test_attach_command_names_the_session_and_refuses_a_bad_name(self):
        self.assertEqual(self.viewer.attach_command("w1", self.host), "/fake/attach w1")
        with self.assertRaisesRegex(SessionError, "^no tmux session 'a b'$"):
            self.viewer.attach_command("a b", self.host)


class CommandViewTest(Base):
    """A Host whose attach target is a CommandView, as Docker's; the caller is in mgr, pane %3."""

    ARGV = ("/usr/bin/env", "-u", "TMUX", "docker", "exec", "-it", "c1", "guard", "--attach")

    def setUp(self):
        super().setUp()
        self.target = CommandView(printed="docker exec -it c1 guard --attach", argv=self.ARGV)
        self.docker = mock.create_autospec(Host, instance=True)
        self.docker.name, self.docker.attachable = "docker", True
        self.docker.attach_target.return_value = self.target
        self.docker.tui_settings.return_value = TuiSettings()
        worker("d1", SID)
        self.docker.list.return_value = ["d1"]
        self.enterContext(environ(HOME=self.home, **INSIDE))
        self.runner.pane = "mgr"
        self.runner.results.update(
            {
                "list-clients": (0, "5\t%9\n"),
                "list-panes": lambda argv: worker_rows() if "-a" in argv else (1,),
                "split-window": split(),
            }
        )

    def test_show_runs_the_hosts_argv_in_a_pane_labeled_by_its_status_line(self):
        """AC1, AC12: the pane runs the Host's argv; status_line labels it."""
        for settings, labeled in ((TuiSettings(), False), (TuiSettings(status_line=True), True)):
            with self.subTest(settings=settings):
                self.runner.calls.clear()
                self.docker.tui_settings.return_value = settings
                self.assertIsNone(self.viewer.show("d1", self.docker, display="mgr/right"))
                command = next(c for c in self.runner.calls if c[1] == "split-window")
                self.assertEqual(command[command.index("--") + 1 :], list(self.ARGV))
                tagged = next(c for c in self.runner.calls if "@worker" in c)
                self.assertEqual(tagged[5:7], ["@worker", "d1"])
                self.assertEqual("pane-border-status" in tagged, labeled)
                self.assertEqual(("@worker-label" in tagged), labeled)
                self.assertNotIn("new-session", [c[1] for c in self.runner.calls])
                self.assertFalse(hidden.read("d1", SID))

    def test_show_outside_tmux_prints_attach_without_opening_a_pane(self):
        self.enterContext(environ(HOME=self.home))
        self.assertEqual(self.viewer.show("d1", self.docker, display="mgr"), self.target.printed)
        self.assertEqual(self.runner.calls, [])
        self.tmux.checked.assert_not_called()

    def test_show_reuses_a_live_worker_pane_and_refuses_a_tmux_session_of_the_name(self):
        self.runner.results["list-panes"] = worker_rows(
            ("%7", "d1", "other", "@1", "mgr", self.target.printed)
        )
        self.assertIsNone(self.viewer.show("d1", self.docker, display="mgr"))
        self.assertNotIn("split-window", [c[1] for c in self.runner.calls])
        self.tmux.has_session.return_value = True
        with self.assertRaisesRegex(SessionError, "^show d1: a host tmux session d1 exists$"):
            self.viewer.show("d1", self.docker)

    def test_a_bad_host_setting_fails_a_show_before_it_splits(self):
        self.docker.tui_settings.side_effect = ConfigError("status_line: want true or false")
        hidden.write("d1", SID, hidden=True, live=None)
        with self.assertRaises(ConfigError):
            self.viewer.show("d1", self.docker, display="mgr")
        self.assertEqual(self.runner.calls, [])
        self.assertTrue(hidden.read("d1", SID))

    def test_hide_without_a_worker_pane_only_marks_it_hidden(self):
        for if_closed in (True, False):
            with self.subTest(if_closed=if_closed):
                hidden.write("d1", SID, hidden=False, live=None)
                self.viewer.hide("d1", self.docker, if_closed=if_closed)
                self.assertTrue(hidden.read("d1", SID))
        self.assertNotIn("kill-pane", [c[1] for c in self.runner.calls])

    def test_attach_command_is_the_printed_command(self):
        self.assertEqual(self.viewer.attach_command("d1", self.docker), self.target.printed)

    def retried(self, *targets, **show):
        """show with attach_target answering `targets` in turn."""
        self.runner.pane = None
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


class NotShowableTest(Base):
    def test_a_host_without_an_attach_target_cannot_show_and_is_never_claimed(self):
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
        self.viewer._unclaimed("p1", plain)
        self.tmux.has_session.assert_not_called()


class UnclaimedTest(Base):
    def test_a_name_a_tmux_session_holds_is_refused(self):
        """AC17: a worker never shares its name with a host tmux session."""
        self.tmux.has_session.return_value = True
        with self.assertRaisesRegex(SessionError, "^show w1: a host tmux session w1 exists$"):
            self.viewer._unclaimed("w1", self.host)
        self.tmux.has_session.return_value = False
        self.viewer._unclaimed("w1", self.host)
        self.tmux.has_session.side_effect = TuiError("tmux: boom")
        with self.assertRaisesRegex(SessionError, "^show w1: tmux: boom$"):
            self.viewer._unclaimed("w1", self.host)
