"""Managed channel and conservative crash admission, without real processes."""

import os
from unittest import mock

from ensemble.session.engine.types import Liveness, Unanswered, WorkerExit, WorkerOutput
from ensemble.session.hosts.tmux import managed
from tests.session.case import Case


class ManagedTest(Case):
    def test_never_started_name_is_absent(self):
        self.assertEqual(managed.liveness("tmux", "worker", "a" * 32), Liveness.ABSENT)

    def test_existing_empty_directory_is_unknown(self):
        directory = managed.location("tmux", "worker")
        managed.private_directory(directory)
        self.assertEqual(managed.liveness("tmux", "worker", "a" * 32), Liveness.UNKNOWN)

    def test_symlink_directory_is_unknown(self):
        directory = managed.location("tmux", "worker")
        managed.private_directory(os.path.dirname(directory))
        os.symlink(self.home, directory)
        self.assertEqual(managed.liveness("tmux", "worker", "a" * 32), Liveness.UNKNOWN)

    def test_safe_json_rejects_links_and_permissions(self):
        path = self.home + "/state"
        managed.save_json(path, {"state": "exited"})
        self.assertEqual(managed.load_json(path), {"state": "exited"})
        os.chmod(path, 0o644)
        with self.assertRaises(OSError):
            managed.load_json(path)
        os.symlink(path, path + "-link")
        with self.assertRaises(OSError):
            managed.load_json(path + "-link")

    def test_ended_generation_requires_matching_record_and_free_lease(self):
        generation = "a" * 32
        directory = managed.location("tmux", "worker")
        managed.private_directory(directory + "/" + generation)
        managed.save_json(directory + "/current.json", {"generation": generation})
        managed.save_json(
            directory + "/" + generation + "/state.json",
            {"generation": generation, "state": "exited"},
        )
        with open(directory + "/" + generation + "/lease", "w"):
            pass
        os.chmod(directory + "/" + generation + "/lease", 0o600)
        self.assertEqual(managed.liveness("tmux", "worker", generation), Liveness.ABSENT)
        self.assertEqual(managed.liveness("tmux", "worker", "b" * 32), Liveness.UNKNOWN)

    def channel(self, *, terminal=False):
        return managed.ManagedProcess(
            "worker", "a" * 32, mock.Mock(), terminal=terminal, tmux=mock.Mock(), start_reader=False
        )

    def test_output_then_exit_once(self):
        channel = self.channel()
        channel.accept(b"s", b'{"pid":123}')
        channel.accept(b"o", b"last reply")
        channel.accept(b"x", b'{"returncode":0}')
        self.assertEqual(channel.read(0), [WorkerOutput(b"last reply"), WorkerExit(0)])
        self.assertEqual(channel.read(0), [])
        self.assertEqual(channel.returncode(), 0)
        with self.assertRaises(ValueError):
            channel.accept(b"o", b"late")

    def test_overflow_fails_and_closes_lifecycle(self):
        channel = self.channel()
        channel.accept(b"s", b'{"pid":123}')
        channel.accept(b"o", b"x" * (managed.QUEUE_MAX + 1))
        with self.assertRaises(Unanswered):
            channel.read(0)
        channel.proc.stdin.close.assert_called_once()
        self.assertEqual(channel.state(), Liveness.UNKNOWN)

    def test_headless_rejects_terminal_operations(self):
        channel = self.channel()
        for operation in (lambda: channel.screen(), lambda: channel.press(())):
            with self.assertRaises(managed.NotShowable):
                operation()

    def test_invalid_frames_and_exit_shape(self):
        for kind, payload in (
            (b"s", b'{"pid":false}'),
            (b"x", b'{"returncode":false}'),
            (b"z", b"{}"),
        ):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.channel().accept(kind, payload)
