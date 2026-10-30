"""TmuxHost over recorded tmux calls: its class and capacity rows, attach targets, a dead
generation's release, a failed listing and its workers whose display is gone (its managed
workers: managed_test.py).
"""

import json
import os
import shlex
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
    Unanswered,
)
from ensemble.session.hosts.base import ACTIVITY, TUI_DIR, TUI_STATE
from ensemble.session.hosts.tmux import TmuxHost, managed
from ensemble.session.hosts.types import CommandView
from ensemble.session.modes.tui import TuiStore
from ensemble.utils import STALE_AGE, load_class
from tests.session.case import Case
from tests.session.tmux_host_support import Base

TuiError = tmux_cli.TuiError
NO_PANE = "no anchor pane: not in tmux"
OTHER = "1f8fad5b-d9cb-469f-a165-70867728950e"


def _worker(case, name, *, host="tmux", state="running", live=True):
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
    managed.save_json(os.path.join(run, "state.json"), {"generation": generation, "state": state})
    lease = managed.lease_file(os.path.join(run, "lease"))
    case.addCleanup(os.close, lease)
    if live:
        managed.fcntl.flock(lease, managed.fcntl.LOCK_EX)
    return directory


class GuardInventoryTest(Case):
    def setUp(self):
        super().setUp()
        self.tmux = mock.create_autospec(TmuxCli, instance=True)
        reached = AssertionError("inventory reached display server")
        self.tmux.run.side_effect = self.tmux.checked.side_effect = reached
        self.tmux.has_session.side_effect = reached
        self.host = TmuxHost("tmux", tmux=self.tmux)

    def test_guard_workers_are_listed_without_a_display_or_harness_record(self):
        _worker(self, "live")
        _worker(self, "unknown", live=False)
        _worker(self, "ended", state="exited", live=False)
        _worker(self, "theirs", host="other")
        self.assertEqual(self.host.list(), ["live", "unknown"])
        rows = self.host.rows()
        self.assertEqual([row.name for row in rows], ["live", "unknown"])
        self.assertEqual([row.state for row in rows], [StatusState.WORKING, StatusState.UNKNOWN])
        self.assertEqual(
            [(row.sid, row.client, row.display) for row in rows],
            [(OTHER, "claude", "manager")] * 2,
        )
        self.assertEqual(self.tmux.mock_calls, [])

    def test_capacity_rows_carry_each_workers_guard_record_generation(self):
        _worker(self, "live")
        _worker(self, "unknown", live=False)
        _worker(self, "ended", state="exited", live=False)
        self.assertEqual(
            [(r.name, r.client, r.state, r.generation) for r in self.host._capacity_rows()],
            [
                ("live", "claude", StatusState.WORKING, "a" * 32),
                ("unknown", "claude", StatusState.UNKNOWN, "a" * 32),
            ],
        )

    def test_an_empty_guard_inventory_never_lists_a_display_session(self):
        self.assertEqual(self.host.list(), [])
        self.assertEqual(self.host.rows(), [])

    def test_exit_diagnostic_is_bound_to_host_name_sid_and_generation_without_listing_dead(self):
        directory = _worker(self, "ended", state="exited", live=False)
        generation = "a" * 32
        path = os.path.join(directory, generation, "diagnostic.json")
        managed.save_json(path, {"text": "final error"})
        self.assertEqual(self.host.last_lines("ended", OTHER, generation), "final error")
        self.assertEqual(self.host.last_lines("ended", OTHER), "final error")
        self.assertEqual(self.host.last_lines("ended", "foreign"), "")
        self.assertEqual(self.host.last_lines("ended", "foreign", generation), "")
        self.assertEqual(self.host.last_lines("ended", OTHER, "b" * 32), "")
        self.assertEqual(TmuxHost("other").last_lines("ended", OTHER, generation), "")
        self.assertEqual(self.host.list(), [])
        os.unlink(path)
        os.symlink(os.path.join(directory, "current.json"), path)
        self.assertEqual(self.host.last_lines("ended", OTHER, generation), "")
        self.assertEqual(self.tmux.mock_calls, [])

    def test_activity_belongs_to_the_guard_generation_without_a_display(self):
        _worker(self, "worker")
        folder = os.path.join(os.path.expanduser(TUI_DIR), "worker")
        os.makedirs(folder)
        for generation, state, expected in (
            ("a" * 32, "done", StatusState.DONE),
            ("a" * 32, "blocked", StatusState.BLOCKED),
            ("older", "done", StatusState.WORKING),
        ):
            with self.subTest(generation=generation, state=state):
                with open(os.path.join(folder, ACTIVITY), "w") as stream:
                    stream.write(f"{generation} {state}\n")
                self.assertEqual(self.host.rows()[0].state, expected)

    def test_guard_inventory_rejects_an_untrusted_root(self):
        root = managed.control_root()
        os.symlink(self.home, root)
        with self.assertRaises(SessionError):
            self.host.list()

    def test_guard_record_cannot_claim_another_workers_directory(self):
        directory = _worker(self, "worker")
        record = managed.load_json(os.path.join(directory, "current.json"))
        managed.save_json(os.path.join(directory, "current.json"), {**record, "name": "forged"})
        with self.assertRaises(SessionError):
            self.host.list()


class ClassTest(Case):
    def test_an_attachable_host_loaded_by_its_config_name(self):
        cls = load_class("ensemble.session.hosts.tmux:TmuxHost", Host)
        self.assertIs(cls, TmuxHost)
        self.assertTrue(cls.attachable)
        host = TmuxHost("tmux", {"status_line": True})
        self.assertEqual((host.name, host.settings), ("tmux", {"status_line": True}))
        self.assertFalse(host._isolated)
        self.assertTrue(TmuxHost("tmux", {"isolated": True})._isolated)
        self.assertIsInstance(host.tmux, TmuxCli)

    def test_client_context_is_the_loopback_and_the_isolated_permission_mode(self):
        self.assertEqual(
            TmuxHost("tmux", {})._client_context(), ClientContext("127.0.0.1", Permission.AUTO)
        )
        self.assertEqual(
            TmuxHost("tmux", {"isolated": True})._client_context(),
            ClientContext("127.0.0.1", Permission.YOLO),
        )


class AttachTargetTest(Base):
    def test_attach_target_uses_the_guard_generation_without_a_display(self):
        directory = _worker(self, "w1")
        target = self.host.attach_target("w1")
        self.assertIsInstance(target, CommandView)
        self.assertEqual(target.argv[-2:], ("--attach", directory + "/" + "a" * 32 + "/display"))
        self.assertEqual(shlex.split(target.printed), list(target.argv))
        self.assertEqual(self.tmux.mock_calls, [])

    def test_attach_refuses_missing_unknown_and_invalid_workers(self):
        with self.assertRaises(SessionError):
            self.host.attach_target("missing")
        _worker(self, "unknown", live=False)
        with self.assertRaises(Unanswered):
            self.host.attach_target("unknown")
        with self.assertRaises(SessionError):
            self.host.attach_target("a b")


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
