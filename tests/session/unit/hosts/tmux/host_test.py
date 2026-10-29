"""TmuxHost over recorded tmux calls: its class and capacity namespace, attach targets, a dead
generation's release, a failed listing and its workers whose display is gone (its managed
workers: managed_test.py).
"""

import json
import os
import time
from types import SimpleNamespace
from unittest import mock

from ensemble.session import Host, SessionError
from ensemble.session.engine import tmux_cli
from ensemble.session.engine.tmux_cli import (
    TmuxCli,
)
from ensemble.session.engine.types import (
    ClientContext,
    Liveness,
    Permission,
    StatusState,
)
from ensemble.session.hosts.base import ACTIVITY, TUI_DIR, TUI_STATE
from ensemble.session.hosts.tmux import TmuxHost
from ensemble.session.hosts.types import SessionView
from ensemble.session.modes.tui import TuiStore
from ensemble.utils import STALE_AGE, load_class
from tests.session.case import Case
from tests.session.tmux_host_support import Base

TuiError = tmux_cli.TuiError
NO_PANE = "no anchor pane: not in tmux"
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


class AttachTargetTest(Base):
    def test_attach_target_is_the_session_itself_and_refuses_a_bad_name(self):
        self.assertTrue(TmuxHost.attachable)
        self.assertEqual(self.host.attach_target("w1"), SessionView("w1"))
        with self.assertRaisesRegex(SessionError, "^no tmux session 'a b'$"):
            self.host.attach_target("a b")


class Release(Base):
    """_release: a dead generation's display, the tmux session of its name, goes only while
    tagged with that generation.
    """

    def shown(self, generation):
        self.tmux.option.side_effect = lambda _target, key: (
            generation if key == "@generation" else None
        )

    def test_the_display_of_that_generation_is_killed(self):
        self.shown("g1")
        self.host._release("w1", "g1")
        self.tmux.kill.assert_called_once_with("w1")

    def test_another_generations_display_or_none_is_left(self):
        for shown in ("g2", None):
            with self.subTest(shown=shown):
                self.shown(shown)
                self.host._release("w1", "g1")
        self.tmux.kill.assert_not_called()

    def test_a_tmux_failure_is_a_driver_error_and_a_bad_name_reaches_no_tmux(self):
        self.shown("g1")
        self.tmux.kill.side_effect = TuiError("tmux: boom")
        with self.assertRaisesRegex(SessionError, r"\Atmux session w1: tmux: boom\Z"):
            self.host._release("w1", "g1")
        self.tmux.reset_mock()
        with self.assertRaisesRegex(SessionError, "^no tmux session 'a b'$"):
            self.host._release("a b", "g1")
        self.assertEqual(self.tmux.mock_calls, [])


class ListTest(Base):
    def worker(self, name, *, host="tmux", generation="g" * 32, mark=None):
        folder = os.path.join(os.path.expanduser(TUI_DIR), name)
        os.makedirs(folder, exist_ok=True)
        state = {"sid": f"sid-{name}", "generation": generation, "client": "claude"}
        with open(os.path.join(folder, TUI_STATE), "w", encoding="utf-8") as file:
            json.dump({**state, "display": "mgr", "host": host}, file)
        if mark is not None:
            with open(os.path.join(folder, ACTIVITY), "w", encoding="utf-8") as file:
                file.write(f"{generation} {mark}\n")

    def test_a_display_less_worker_is_listed_from_its_worker_dir(self):
        """A5: a live worker whose tmux session was killed is still listed, its state from its
        activity; an ABSENT one, another host's or a displayed one's dir adds no row.
        """
        self.worker("live", mark="done")
        self.worker("busy")
        self.worker("lost")
        self.worker("dead")
        self.worker("theirs", host="docker")
        self.worker("shown")
        self.tmux.listing.return_value = [
            {
                "name": "shown",
                "sid": "sid-shown",
                "client": "claude",
                "dead": False,
                "state": None,
                "display": "mgr",
                "last_lines": None,
            }
        ]
        found = {
            "live": Liveness.LIVE,
            "busy": Liveness.LIVE,
            "lost": Liveness.UNKNOWN,
            "dead": Liveness.ABSENT,
        }
        with mock.patch.object(
            self.host, "worker_liveness", side_effect=lambda name, _generation: found[name]
        ):
            self.assertEqual(self.host.list(), ["busy", "live", "lost", "shown"])
            rows = {row.name: row for row in self.host.rows()}
        self.assertEqual(sorted(rows), ["busy", "live", "lost", "shown"])
        self.assertEqual(
            [
                (rows[n].state, rows[n].sid, rows[n].client, rows[n].display)
                for n in found
                if n in rows
            ],
            [
                (StatusState.DONE, "sid-live", "claude", "mgr"),
                (StatusState.WORKING, "sid-busy", "claude", "mgr"),
                (StatusState.UNKNOWN, "sid-lost", "claude", "mgr"),
            ],
        )
        self.assertEqual((rows["live"].shown, rows["live"].last_lines), (None, None))

    def test_the_sweep_still_expires_a_dead_display_less_worker_dir(self):
        self.worker("dead")
        folder = os.path.join(os.path.expanduser(TUI_DIR), "dead")
        old = time.time() - STALE_AGE - 60
        for path in (os.path.join(folder, TUI_STATE), folder):
            os.utime(path, (old, old))
        with mock.patch.object(self.host, "worker_liveness", return_value=Liveness.ABSENT):
            TuiStore.attach("dead", host=self.host).expire(set(self.host.list()))
        self.assertFalse(os.path.exists(folder))

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
