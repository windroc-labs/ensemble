"""The Viewer's display functions: FORMAT, --display, layout, rows, names, tile, started and
status.
"""

import json
import os
from unittest import mock

from ensemble.session import ConfigError, SessionError
from ensemble.session.engine import tmux_cli
from ensemble.session.engine.tmux_cli import TmuxCli, TuiError
from ensemble.session.engine.types import SessionRow
from ensemble.session.hosts.base import Host
from ensemble.session.hosts.types import CommandView
from ensemble.session.viewer import display as displays
from ensemble.session.viewer import hidden
from ensemble.session.viewer.viewer import Viewer, fold, unfold
from ensemble.utils import Status, StatusState
from tests.session.case import Case
from tests.session.hosted_support import SID
from tests.session.tmux_fixtures import INSIDE, SESSIONS, FakeTmux, TileFixture, environ, hold
from tests.session.tmux_fixtures import expected_row as listed
from tests.session.tmux_host_support import Base

NO_PANE = "no anchor pane: not in tmux"
OTHER = "1f8fad5b-d9cb-469f-a165-70867728950e"
FOLDS = (
    (("mgr", None, None, False), "mgr"),
    (("mgr", "right", None, False), "mgr/right"),
    (("mgr", "below", "m1", False), "mgr/below/m1"),
    ((None, "below", None, False), "/below"),
    ((None, None, "m1", False), "//m1"),
    ((None, "right", "m1", False), "/right/m1"),
    ((None, None, None, False), None),
    (("mgr", None, None, True), "mgr///hidden"),
    ((None, "below", "m1", True), "/below/m1/hidden"),
    ((None, None, None, True), "///hidden"),
)
FOREIGN = (
    "elsewhere:1.2",
    "mgr/left",
    "a b",
    "mgr/right/a b",
    "a/b/c/d",
    "x:y:z",
    "a/b/c/",
    "mgr///hidden/x",
    "mgr///shown",
)
MARKED = "display {!r}: its hidden part comes only from none"


class FoldTest(Case):
    def test_fold_and_unfold_round_trip(self):
        for parts, display in FOLDS:
            with self.subTest(parts=parts):
                self.assertEqual(fold(*parts[:3], hidden=parts[3]), display)
                self.assertEqual(unfold(display), parts)

    def test_unfold_of_none_is_no_opener_started_hidden(self):
        self.assertEqual(unfold("none"), (None, None, None, True))

    def test_unfold_takes_empty_parts_as_absent(self):
        for display, parts in (
            ("", (None, None, None, False)),
            ("mgr/", ("mgr", None, None, False)),
        ):
            with self.subTest(display=display):
                self.assertEqual(unfold(display), parts)

    def test_unfold_of_a_display_not_in_the_format_is_none(self):
        for display in (*FOREIGN, "mgr\t/right", "mgr;", "mgr/right;", 5):
            with self.subTest(display=display):
                self.assertIsNone(unfold(display))

    def test_fold_refuses_a_bad_part_with_the_drivers_messages(self):
        for parts, message in (
            (
                ("x y", None, None),
                "opener must be a tmux session name, got 'x y'",
            ),
            ((None, "left", None), "split must be right or below, got 'left'"),
            ((None, None, "a b"), "split_from must match [A-Za-z0-9_-]+, got 'a b'"),
            ((None, None, 5), "split_from must match [A-Za-z0-9_-]+, got 5"),
        ):
            with self.subTest(parts=parts), self.assertRaises(ConfigError) as caught:
                fold(*parts)
            self.assertEqual(str(caught.exception), message)


class DisplayTest(Base):
    def test_display_help_names_the_format(self):
        help_text = displays.display_help(self.host)
        self.assertIn("<opener>[/<split>[/<split_from>[/hidden]]]", help_text)
        self.assertIn("right or below", help_text)
        self.assertIn("none: start without a pane, the caller still its opener", help_text)

    def test_display_none_is_the_callers_opener_started_hidden(self):
        self.runner.pane = "mgr"
        self.enterContext(environ(**INSIDE))
        self.assertEqual(displays.display(self.viewer, "none", self.host), "mgr///hidden")
        self.enterContext(environ())
        self.assertEqual(displays.display(self.viewer, "none", self.host), "///hidden")

    def test_a_given_hidden_part_is_refused(self):
        self.enterContext(environ(**INSIDE))
        for value in ("mgr///hidden", "///hidden", "/below/m1/hidden"):
            with self.subTest(value=value):
                for call in (
                    lambda v: displays.display(self.viewer, v, self.host),
                    lambda v: displays.layout(self.viewer, self.host, v),
                ):
                    with self.assertRaises(ConfigError) as caught:
                        call(value)
                    self.assertEqual(str(caught.exception), MARKED.format(value))
        self.assertEqual(self.runner.calls, [])

    def test_layout_of_none_needs_no_anchor(self):
        self.runner.pane = "mgr"
        self.enterContext(environ(**INSIDE))
        self.assertEqual(displays.layout(self.viewer, self.host, "none"), "mgr///hidden")
        self.assertNotIn("list-clients", [call[1] for call in self.runner.calls])
        self.enterContext(environ())
        self.assertEqual(displays.layout(self.viewer, self.host, "none"), "///hidden")

    def test_the_callers_opener_fills_an_empty_or_absent_opener_part(self):
        self.runner.pane = "mgr"
        self.enterContext(environ(**INSIDE))
        for value, want in (
            (None, "mgr"),
            ("", "mgr"),
            ("/right", "mgr/right"),
            ("//m1", "mgr//m1"),
        ):
            with self.subTest(value=value):
                self.assertEqual(displays.display(self.viewer, value, self.host), want)

    def test_a_given_opener_or_a_foreign_value_is_returned_as_given(self):
        self.enterContext(environ(**INSIDE))
        for value in ("w1", "w1/below/m1", *FOREIGN):
            with self.subTest(value=value):
                self.assertEqual(displays.display(self.viewer, value, self.host), value)
        self.assertEqual(self.runner.calls, [])

    def test_no_opener_available_returns_the_value_unchanged(self):
        self.enterContext(environ())
        for value in (None, "", "/right"):
            with self.subTest(value=value):
                self.assertEqual(displays.display(self.viewer, value, self.host), value)

    def test_a_display_tmux_could_not_list_is_a_config_error(self):
        self.enterContext(environ(**INSIDE))
        for value in ("mgr\tx", "mgr\nx", "mgr;", 5):
            with self.subTest(value=value), self.assertRaises(ConfigError) as caught:
                displays.display(self.viewer, value, self.host)
            self.assertEqual(str(caught.exception), tmux_cli.display_error(value))
        self.assertEqual(self.runner.calls, [])


class DescendantsTest(Base):
    def test_the_matching_sessions_then_those_each_opened_transitively(self):
        self.host = _plain_host(
            [
                _row("a", "mgr"),
                _row("b", "a/below"),
                _row("c", "b"),
                _row("d", "d"),
                _row("x", "other"),
            ]
        )
        self.host.attachable = True
        self.tmux.listing.return_value = [
            listed("a", opener="mgr", display="mgr"),
            listed("b", opener="a", display="a/below"),
            listed("c", opener="b", display="b"),
            listed("d", opener="d", display="d"),
            listed("x", opener="other", display="other"),
        ]
        self.assertEqual(displays.names(self.viewer, self.host, "mgr"), ["a"])
        self.assertEqual(
            displays.names(self.viewer, self.host, "mgr", descendants=True), ["a", "b", "c"]
        )
        self.assertEqual(displays.names(self.viewer, self.host, "other", descendants=True), ["x"])
        self.assertEqual(displays.names(self.viewer, self.host, "", descendants=True), [])

    def test_a_tmux_failure_is_a_driver_error(self):
        self.host = _plain_host([_row("a", "mgr")])
        self.host.attachable = True
        self.tmux.listing.side_effect = TuiError("tmux list-sessions exited 1: lost server")
        with self.assertRaisesRegex(
            SessionError, r"\Atmux sessions unknown: tmux list-sessions exited 1: lost server\Z"
        ):
            displays.names(self.viewer, self.host, "mgr", descendants=True)


class RowsTest(Base):
    def test_only_worker_rows_get_display_placement_without_tmux_identity(self):
        self.enterContext(environ(**INSIDE))
        self.runner.pane = "manager"
        workers = [_row("w1", "manager"), _row("w2", "manager")]
        self.host = _plain_host(workers)
        self.host.attachable = True
        self.tmux.listing.return_value = [
            listed("manager"),
            listed("w1", sid="forged", client="other", state="dead", shown="manager"),
        ]
        rows = displays.rows(self.viewer, self.host, None)
        self.assertEqual([row.name for row in rows], ["w1", "w2"])
        self.assertEqual([row.state for row in rows], [StatusState.WORKING] * 2)
        self.assertEqual([row.shown for row in rows], ["manager", None])
        self.assertEqual(
            [(row.sid, row.client, row.display) for row in rows],
            [(row.sid, row.client, row.display) for row in workers],
        )


class TileTest(TileFixture):
    """tile over a fake runner (TileFixture's window @3)."""

    def tile(self, fake, *args, **kw):
        displays.tile(Viewer(tmux=TmuxCli(fake)), *args, **kw)
        return fake

    def test_tile_lays_out_the_window_unless_its_pane_set_is_unchanged(self):
        fake = self.tile(self.fake(*self.WIDE), "@3")
        self.assertEqual(
            fake.calls,
            [self.SOCKET, self.READ, self.PANES, SESSIONS, *self.applied(self.WIDE_SENT)],
        )
        fake = self.tile(self.fake(*self.WIDE), "@3", if_panes_changed=True)
        self.assertEqual(fake.calls, [self.SOCKET, self.READ, self.PANES, self.UNMUTE])

    def test_tile_without_wait_returns_while_another_tile_holds_the_lock(self):
        hold(self, self.lock)
        fake = self.tile(self.fake(*self.WIDE), "@3", wait=False)
        self.assertEqual(fake.calls, [self.SOCKET])

    def test_a_failed_tile_is_a_session_error(self):
        with self.assertRaisesRegex(SessionError, "^tile @3: tmux: boom$"):
            self.tile(FakeTmux(fail=("display-message",)), "@3")


def _plain_host(rows=()):
    plain = mock.create_autospec(Host, instance=True)
    plain.name, plain.attachable = "plain", False
    plain.attach_target.return_value = None
    plain.rows.return_value = list(rows)
    plain.list.return_value = [row.name for row in rows]
    return plain


def _row(name, display):
    return SessionRow(name, None, None, StatusState.WORKING, display, None, None, own=False)


class PlainHostTest(Base):
    def test_a_plain_hosts_display_is_kept(self):
        plain = _plain_host()
        for value in (None, "x", "mgr", "none", "a b;"):
            with self.subTest(value=value):
                self.assertEqual(displays.display(self.viewer, value, plain), value)
        with self.assertRaisesRegex(ConfigError, "^display 5: want text$"):
            displays.display(self.viewer, 5, plain)
        self.assertIsNone(displays.layout(self.viewer, plain, "/below"))
        self.assertEqual(displays.display_help(plain), "")
        self.assertEqual(self.runner.calls, [])

    def test_a_plain_hosts_display_lists_only_its_equal_rows_as_they_are(self):
        rows = [_row("p1", "mgr"), _row("p2", "mgr/right"), _row("p3", None)]
        plain = _plain_host(rows)
        self.assertEqual(displays.rows(self.viewer, plain, "mgr"), rows[:1])
        self.assertEqual(displays.rows(self.viewer, plain, None), rows)
        self.assertEqual(displays.rows(self.viewer, plain, ""), [])
        self.assertEqual(displays.names(self.viewer, plain, "mgr", descendants=True), ["p1"])
        self.tmux.listing.assert_not_called()


class HostFailureTest(Base):
    def test_a_filtered_listing_fails_when_its_host_does_though_its_rows_only_warn(self):
        for attachable in (False, True):
            host = _plain_host()
            host.attachable = attachable
            host.list.side_effect = SessionError("docker failed")
            for descendants in (False, True):
                with (
                    self.subTest(attachable=attachable, descendants=descendants),
                    self.assertRaisesRegex(SessionError, "^docker failed$"),
                ):
                    displays.names(self.viewer, host, "mgr", descendants=descendants)


class OpenerTest(Base):
    def test_opener_falls_back_to_display(self):
        self.host = _plain_host(
            [_row("a", "mgr/right"), _row("b", "a/below"), _row("c", None), _row("d", "d")]
        )
        self.host.attachable = True
        self.tmux.listing.return_value = [
            listed("a", display="mgr/right"),
            listed("b", display="a/below"),
            listed("c", opener="b"),
            listed("d", display="d"),
        ]
        self.assertEqual(
            displays.names(self.viewer, self.host, "mgr", descendants=True), ["a", "b", "c"]
        )

    def test_a_view_sessions_live_opener_and_shown_fill_its_row(self):
        docker = _plain_host([_row("d1", "other"), _row("d2", "x")])
        docker.attachable = True
        self.tmux.listing.return_value = [listed("d1", opener="mgr", shown="m")]
        self.assertEqual(displays.names(self.viewer, docker, "d1", descendants=True), [])
        self.assertEqual(displays.names(self.viewer, docker, "mgr", descendants=False), [])
        docker.rows.return_value = [_row("d1", "mgr"), _row("d2", "d1/below")]
        self.assertEqual(displays.names(self.viewer, docker, "mgr", descendants=True), ["d1", "d2"])
        self.assertEqual(
            [row.shown for row in displays.rows(self.viewer, docker, None)], ["m", None]
        )


class StatusTest(Base):
    """A live session's Status from its sid and activity word (its view's), hidden and attach
    from the Viewer.
    """

    def status(self, **fields):
        return Status(
            **{
                "name": "w1",
                "alive": True,
                "state": StatusState.WORKING,
                "outcome": None,
                "attach": None,
                "hidden": False,
                **fields,
            }
        )

    def test_its_state_is_its_activity_else_working(self):
        for word in (None, StatusState.DONE, StatusState.BLOCKED):
            with self.subTest(word=word):
                self.assertEqual(
                    displays.status(self.viewer, _plain_host(), "w1", SID, word),
                    self.status(state=word or StatusState.WORKING),
                )

    def test_hidden_and_attach_come_from_the_viewer(self):
        self.host = _plain_host()
        self.host.attachable = True
        self.host.attach_target.return_value = CommandView("tmux attach -t '=w1'", ("attach",))
        hidden.write("w1", SID, hidden=True, live=None)
        self.assertEqual(
            displays.status(self.viewer, self.host, "w1", SID, None),
            self.status(hidden=True, attach="tmux attach -t '=w1'"),
        )
        for sid in (OTHER, None):
            self.assertFalse(displays.status(self.viewer, self.host, "w1", sid, None).hidden)
        self.assertIsNone(displays.status(self.viewer, _plain_host(), "w1", SID, None).attach)

    def test_a_session_error_is_the_unknown_status(self):
        host = mock.create_autospec(Host, instance=True)
        host.name, host.attachable = "docker", True
        host.attach_target.side_effect = SessionError("no docker session w1")
        self.assertEqual(
            displays.status(self.viewer, host, "w1", SID, StatusState.DONE),
            self.status(alive=False, state=StatusState.UNKNOWN),
        )


class StartedTest(Base):
    """The sid of its worker state: SID unless set."""

    def setUp(self):
        super().setUp()
        self.worker(SID)

    def worker(self, sid) -> None:
        folder = os.path.join(self.ensemble, "tui", "t1")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "session.json"), "w", encoding="utf-8") as f:
            json.dump({"sid": sid, "generation": "g1"}, f)

    def test_a_none_display_marks_a_new_sid_hidden_only(self):
        displays.started(self.viewer, "t1", self.host, "mgr///hidden")
        self.assertTrue(hidden.read("t1", SID))
        hidden.write("t1", SID, hidden=False, live=None)
        displays.started(self.viewer, "t1", self.host, "mgr///hidden")
        self.assertFalse(hidden.read("t1", SID))

    def test_a_shown_display_a_sidless_session_or_a_plain_host_writes_no_entry(self):
        displays.started(self.viewer, "t1", self.host, "mgr")
        displays.started(self.viewer, "t1", _plain_host(), "none")
        self.worker(None)
        displays.started(self.viewer, "t1", self.host, "mgr///hidden")
        os.unlink(os.path.join(self.ensemble, "tui", "t1", "session.json"))
        displays.started(self.viewer, "t1", self.host, "mgr///hidden")  # no worker state
        self.assertFalse(os.path.exists(os.path.join(self.ensemble, "hidden")))

    def test_a_failed_hidden_write_is_a_session_error(self):
        store = os.path.join(self.ensemble, "hidden")
        os.mkdir(store, 0o500)
        self.addCleanup(os.chmod, store, 0o700)
        with self.assertRaisesRegex(
            SessionError, "^tui session t1: hidden state: Permission denied"
        ):
            displays.started(self.viewer, "t1", self.host, "///hidden")
