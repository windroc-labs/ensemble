"""TmuxHost over recorded tmux calls: its class and capacity namespace, attach targets, a dead
generation's release, a failed listing and its workers whose display is gone (its managed
workers: managed_test.py).
"""

import json
import os
import time
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
from ensemble.session.hosts.tmux import TmuxHost, managed
from ensemble.session.hosts.types import SessionView
from ensemble.session.modes.tui import TuiStore
from ensemble.utils import STALE_AGE, load_class
from tests.session.case import Case
from tests.session.tmux_host_support import Base

TuiError = tmux_cli.TuiError
NO_PANE = "no anchor pane: not in tmux"
OTHER = "1f8fad5b-d9cb-469f-a165-70867728950e"


class GuardInventoryTest(Case):
    def setUp(self):
        super().setUp()
        self.tmux = mock.create_autospec(TmuxCli, instance=True)
        self.tmux.listing.side_effect = AssertionError("inventory reached display server")
        self.host = TmuxHost("tmux", tmux=self.tmux)

    def worker(self, name, *, host="tmux", state="running", live=True):
        generation = "a" * 32
        directory = managed.location(host, name)
        run = os.path.join(directory, generation)
        managed.private_directory(run)
        managed.save_json(
            os.path.join(directory, "current.json"),
            {
                "host": host,
                "name": name,
                "generation": generation,
                "sid": OTHER,
                "client": "claude",
                "display": "manager",
            },
        )
        managed.save_json(
            os.path.join(run, "state.json"), {"generation": generation, "state": state}
        )
        lease = managed.lease_file(os.path.join(run, "lease"))
        self.addCleanup(os.close, lease)
        if live:
            managed.fcntl.flock(lease, managed.fcntl.LOCK_EX)
        return directory

    def test_guard_workers_are_listed_without_a_display_or_harness_record(self):
        self.worker("live")
        self.worker("unknown", live=False)
        self.worker("ended", state="exited", live=False)
        self.worker("theirs", host="other")
        self.assertEqual(self.host.list(), ["live", "unknown"])
        rows = self.host.rows()
        self.assertEqual([row.name for row in rows], ["live", "unknown"])
        self.assertEqual([row.state for row in rows], [StatusState.WORKING, StatusState.UNKNOWN])
        self.assertEqual(
            [(row.sid, row.client, row.display) for row in rows],
            [(OTHER, "claude", "manager")] * 2,
        )
        self.assertEqual(self.tmux.mock_calls, [])

    def test_an_empty_guard_inventory_never_lists_a_display_session(self):
        self.assertEqual(self.host.list(), [])
        self.assertEqual(self.host.rows(), [])
        self.assertEqual(self.tmux.mock_calls, [])

    def test_guard_inventory_rejects_an_untrusted_root(self):
        root = managed.control_root()
        os.symlink(self.home, root)
        with self.assertRaises(SessionError):
            self.host.list()

    def test_guard_record_cannot_claim_another_workers_directory(self):
        directory = self.worker("worker")
        record = managed.load_json(os.path.join(directory, "current.json"))
        managed.save_json(os.path.join(directory, "current.json"), {**record, "name": "forged"})
        with self.assertRaises(SessionError):
            self.host.list()


class ClassTest(Case):
    def test_capacity_namespace_tracks_guard_location_and_host_not_display_settings(self):
        tmux = mock.create_autospec(TmuxCli, instance=True)
        first = TmuxHost("first", {"status_line": False}, tmux=tmux)
        same = TmuxHost("first", {"status_line": True}, tmux=tmux)
        other = TmuxHost("other", tmux=tmux)
        self.assertEqual(first._capacity_namespace(), same._capacity_namespace())
        self.assertNotEqual(first._capacity_namespace(), other._capacity_namespace())
        namespace = first._capacity_namespace()
        with mock.patch.dict(os.environ, {"ENSEMBLE_CONTROL_ROOT": self.home + "/elsewhere"}):
            self.assertNotEqual(first._capacity_namespace(), namespace)
        self.assertEqual(tmux.mock_calls, [])

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

    def test_the_sweep_still_expires_a_dead_display_less_worker_dir(self):
        self.worker("dead")
        folder = os.path.join(os.path.expanduser(TUI_DIR), "dead")
        old = time.time() - STALE_AGE - 60
        for path in (os.path.join(folder, TUI_STATE), folder):
            os.utime(path, (old, old))
        with mock.patch.object(self.host, "worker_liveness", return_value=Liveness.ABSENT):
            TuiStore.attach("dead", host=self.host).expire(set(self.host.list()))
        self.assertFalse(os.path.exists(folder))
