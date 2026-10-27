"""Managed channel and conservative crash admission, without real processes."""

import os
import sys
import threading
from unittest import mock

from ensemble.session.engine.tmux_cli import TuiError
from ensemble.session.engine.types import (
    Launch,
    Liveness,
    SessionBusy,
    StatusState,
    Unanswered,
)
from ensemble.session.harness import guard
from ensemble.session.hosts.base import GUARD_WAIT
from ensemble.session.hosts.tmux import managed
from ensemble.session.hosts.tmux.host import TmuxHost
from tests.session.case import Case


class ManagedTest(Case):
    def test_attacker_owned_or_symlinked_user_root_is_never_absent(self):
        directory = managed.location("tmux", "worker")
        root = os.path.dirname(directory)
        os.symlink(self.home, root)
        self.assertEqual(managed.liveness("tmux", "worker", "a" * 32), Liveness.UNKNOWN)
        with mock.patch.object(managed.subprocess, "Popen") as spawn, self.assertRaises(OSError):
            TmuxHost("tmux", tmux=mock.Mock()).worker(
                "worker", Launch((sys.executable,), cwd=self.home), terminal=False
            )
        spawn.assert_not_called()
        os.unlink(root)
        os.mkdir(root, 0o777)
        os.chmod(root, 0o777)
        self.assertEqual(managed.liveness("tmux", "worker", "a" * 32), Liveness.UNKNOWN)

    def test_failed_spawn_records_no_worker_and_permits_later_start(self):
        host = TmuxHost("tmux", tmux=mock.Mock())
        with (
            mock.patch.object(managed.subprocess, "Popen", side_effect=FileNotFoundError),
            self.assertRaises(managed.SessionError),
        ):
            host.worker("worker", Launch((sys.executable,), cwd=self.home), terminal=False)
        current = managed.load_json(managed.location("tmux", "worker") + "/current.json")
        self.assertEqual(host.worker_liveness("worker", current["generation"]), Liveness.ABSENT)

    def test_reader_start_failure_closes_both_pipe_ends_and_reaps_guard(self):
        proc = mock.Mock()
        host = TmuxHost("tmux", tmux=mock.Mock())
        with (
            mock.patch.object(managed.subprocess, "Popen", return_value=proc),
            mock.patch.object(os, "set_blocking"),
            mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("no threads")),
            self.assertRaisesRegex(RuntimeError, "no threads"),
        ):
            host.worker("worker", Launch((sys.executable,), cwd=self.home), terminal=False)
        proc.stdin.close.assert_called_once()
        proc.stdout.close.assert_called_once()
        proc.wait.assert_called_once_with(timeout=GUARD_WAIT)

    def test_live_lease_and_unclean_guard_death_refuse_second_owner(self):
        generation = "a" * 32
        directory = managed.location("tmux", "worker")
        managed.private_directory(directory + "/" + generation)
        managed.save_json(directory + "/current.json", {"generation": generation})
        managed.save_json(
            directory + "/" + generation + "/state.json",
            {"generation": generation, "state": "running"},
        )
        lease = managed.lease_file(directory + "/" + generation + "/lease")
        try:
            managed.fcntl.flock(lease, managed.fcntl.LOCK_EX)
            self.assertEqual(managed.liveness("tmux", "worker", generation), Liveness.LIVE)
        finally:
            os.close(lease)
        self.assertEqual(managed.liveness("tmux", "worker", generation), Liveness.UNKNOWN)
        with self.assertRaises(SessionBusy):
            TmuxHost("tmux", tmux=mock.Mock()).worker(
                "worker", Launch((sys.executable,), cwd=self.home), terminal=False
            )

    def test_bad_generation_and_missing_record_refuse_before_spawn(self):
        host = TmuxHost("tmux", tmux=mock.Mock())
        with self.assertRaises(managed.ConfigError):
            host.worker(
                "worker",
                Launch((sys.executable,), cwd=self.home),
                terminal=False,
                metadata={"generation": "../other"},
            )
        managed.private_directory(managed.location("tmux", "worker"))
        with mock.patch.object(managed.subprocess, "Popen") as spawn, self.assertRaises(Unanswered):
            host.worker("worker", Launch((sys.executable,), cwd=self.home), terminal=False)
        spawn.assert_not_called()

    def test_worker_start_inherits_only_lease_into_guard(self):
        proc = mock.Mock()
        proc.stdout.read.side_effect = [
            guard.json_frame(b"s", {"pid": 123}),
            guard.json_frame(b"x", {"returncode": 0}),
            b"",
        ]
        host = TmuxHost("tmux", tmux=mock.Mock())
        with (
            mock.patch.object(managed.subprocess, "Popen", return_value=proc) as spawn,
            mock.patch.object(os, "set_blocking"),
        ):
            channel = host.worker(
                "worker", Launch((sys.executable,), cwd=self.home), terminal=False
            )
            channel.close()
        options = spawn.call_args.kwargs
        self.assertTrue(options["close_fds"])
        self.assertEqual(len(options["pass_fds"]), 1)
        self.assertEqual(options["env"]["ENSEMBLE_GUARD_LEASE_FD"], str(options["pass_fds"][0]))
        self.assertIn("--hook-socket", spawn.call_args.args[0])
        self.assertNotIn("--display-grace", spawn.call_args.args[0])
        self.assertIsNone(channel.record())
        self.assertIsNone(channel.display())

    def test_terminal_worker_guard_awaits_the_display_it_starts(self):
        """tmux starts the guard's display at launch, so its guard keeps the display grace."""
        proc = mock.Mock()
        proc.stdout.read.side_effect = [
            guard.json_frame(b"s", {"pid": 123}),
            guard.json_frame(b"x", {"returncode": 0}),
            b"",
        ]
        host = TmuxHost("tmux", tmux=mock.Mock())
        with (
            mock.patch.object(managed.subprocess, "Popen", return_value=proc) as spawn,
            mock.patch.object(os, "set_blocking"),
        ):
            channel = host.worker("worker", Launch((sys.executable,), cwd=self.home), terminal=True)
            channel.close()
        argv = spawn.call_args.args[0]
        start = argv.index("--terminal")
        self.assertEqual(
            argv[start : start + 3], ["--terminal", "--display-grace", "--display-socket"]
        )

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

    def test_display_teardown_kills_only_its_generation(self):
        tmux = mock.Mock()
        channel = managed.TmuxManagedProcess(
            "worker", "a" * 32, mock.Mock(), terminal=True, tmux=tmux, start_reader=False
        )
        tmux.option.return_value = "b" * 32
        channel.close_display()
        tmux.kill.assert_not_called()
        tmux.option.return_value = "a" * 32
        channel.close_display()
        tmux.kill.assert_called_once_with("worker")

    def spawned_env(self, launch: Launch, **environ) -> dict:
        """The guard's environment for `launch`, the harness's environment holding `environ`."""
        proc = mock.Mock()
        proc.stdout.read.side_effect = [
            guard.json_frame(b"s", {"pid": 123}),
            guard.json_frame(b"x", {"returncode": 0}),
            b"",
        ]
        host = TmuxHost("tmux", tmux=mock.Mock())
        with (
            mock.patch.dict(os.environ, environ),
            mock.patch.object(managed.subprocess, "Popen", return_value=proc) as spawn,
            mock.patch.object(os, "set_blocking"),
        ):
            host.worker("worker", launch, terminal=False).close()
        return spawn.call_args.kwargs["env"]

    def test_launch_env_verbatim_without_tmux(self):
        env = self.spawned_env(
            Launch(
                (sys.executable,),
                env={"KEEP": "1", "TMUX": "/tmp/t,1,0", "TMUX_PANE": "%1", "TERM": "screen"},
                cwd=self.home,
            ),
            HARNESS_ONLY="secret",
        )
        self.assertNotIn("HARNESS_ONLY", env)
        self.assertNotIn("TMUX", env)
        self.assertNotIn("TMUX_PANE", env)
        self.assertEqual(env["KEEP"], "1")
        self.assertEqual(env["TERM"], "screen")
        self.assertEqual(
            set(env) - {"KEEP", "TERM"},
            {"ENSEMBLE_GUARD_LEASE_FD", "ENSEMBLE_GUARD_HOOK_SOCKET"},
        )

    def test_term_default_only_when_absent(self):
        env = self.spawned_env(Launch((sys.executable,), env={}, cwd=self.home), TERM="dumb")
        self.assertEqual(env["TERM"], "xterm-256color")

    def test_mark_and_activity_use_own_display(self):
        """Status marks go to this generation's display pane; another generation's is left alone."""
        channel = managed.TmuxManagedProcess(
            "worker", "a" * 32, mock.Mock(), terminal=True, tmux=mock.Mock(), start_reader=False
        )
        process = "4242 $3 %7 5555"
        options = {"@generation": "a" * 32, "@process": process, "@state": "working"}
        channel.tmux.option.side_effect = lambda _target, key: options.get(key)
        channel.tmux.process.return_value = process
        self.assertEqual(channel.activity(), StatusState.WORKING)
        channel.mark(StatusState.DONE, "/events")
        channel.tmux.mark.assert_called_once_with("done", "/events", pane="%7", session=None)
        channel.tmux.mark.reset_mock()
        options["@generation"] = "b" * 32
        self.assertIsNone(channel.activity())
        channel.mark(StatusState.DONE, "/events")
        channel.tmux.mark.assert_not_called()
        options["@generation"] = "a" * 32
        channel.tmux.option.side_effect = TuiError("tmux: no server")
        self.assertIsNone(channel.activity())
        channel.mark(StatusState.DONE, "/events")
        channel.tmux.mark.assert_not_called()
