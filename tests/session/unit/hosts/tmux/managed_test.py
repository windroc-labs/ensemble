"""Managed channel and conservative crash admission, without real processes."""

import os
import sys
import tempfile
import threading
import time
from unittest import mock

from ensemble.session.engine.tmux_cli import TuiError
from ensemble.session.engine.types import (
    ConfigError,
    Launch,
    Liveness,
    PaneStatus,
    SessionBusy,
    SessionError,
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
            host.worker("worker", Launch((sys.executable,), cwd=self.home), terminal=True)
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
                "worker", Launch((sys.executable,), cwd=self.home), terminal=True
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
            host.worker("worker", Launch((sys.executable,), cwd=self.home), terminal=True)
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

    def reopener(self, *, terminal: bool = True):
        """A running handle of generation a…a, its tmux and display opener mocked."""
        tmux, opener = mock.Mock(), mock.Mock()
        channel = managed.TmuxManagedProcess(
            "worker",
            "a" * 32,
            mock.Mock(),
            terminal=terminal,
            tmux=tmux,
            opener=opener if terminal else None,
            start_reader=False,
        )
        return channel, tmux, opener

    def test_redisplay_opens_only_a_gone_or_dead_own_display(self):
        channel, tmux, opener = self.reopener()
        tmux.option.return_value = "a" * 32
        tmux.status.return_value = PaneStatus.RUNNING
        self.assertFalse(channel.redisplay())
        opener.assert_not_called()
        tmux.kill.assert_not_called()
        tmux.status.return_value = -9  # its attach killed, its pane kept
        self.assertTrue(channel.redisplay())
        tmux.kill.assert_called_once_with("worker")
        opener.assert_called_once_with()
        tmux.kill.reset_mock()
        opener.reset_mock()
        tmux.option.return_value = None  # its session killed
        self.assertTrue(channel.redisplay())
        tmux.kill.assert_not_called()
        opener.assert_called_once_with()

    def test_redisplay_reopens_a_dead_display_and_marks_nothing(self):
        """Its state lives in the worker dir (its harness's mark): no tmux mark, no events line."""
        channel, tmux, opener = self.reopener()
        options = {"@generation": "a" * 32, "@process": "4242 $3 %7 5555"}
        tmux.option.side_effect = lambda _target, key: options.get(key)
        opener.side_effect = lambda: options.update({"@process": "4243 $4 %8 5556"})
        tmux.status.return_value = -9
        self.assertTrue(channel.redisplay())
        opener.assert_called_once_with()
        tmux.mark.assert_not_called()

    def test_redisplay_refuses_and_leaves_alone_what_it_cannot_tell(self):
        channel, tmux, opener = self.reopener()
        tmux.option.return_value = "b" * 32
        with self.assertRaisesRegex(
            SessionError, "^worker worker: its name shows another display$"
        ):
            channel.redisplay()
        tmux.option.return_value = "a" * 32
        tmux.status.return_value = PaneStatus.UNKNOWN
        with self.assertRaisesRegex(Unanswered, "^worker worker: its display unknown$"):
            channel.redisplay()
        tmux.option.side_effect = TuiError("tmux: no server")
        with self.assertRaisesRegex(SessionError, "^worker worker: display: tmux: no server$"):
            channel.redisplay()
        tmux.kill.assert_not_called()
        opener.assert_not_called()
        tmux.option.side_effect = None
        tmux.option.return_value = None
        opener.side_effect = TuiError("session worker exists")
        with self.assertRaisesRegex(
            SessionError, "^worker worker: display: session worker exists$"
        ):
            channel.redisplay()

    def test_redisplay_of_an_exited_or_failed_worker_is_refused(self):
        channel, tmux, opener = self.reopener()
        channel.code = 0
        with self.assertRaisesRegex(SessionError, "^worker worker: exited$"):
            channel.redisplay()
        channel, tmux, opener = self.reopener()
        channel.failure = "guard channel: closed"
        with self.assertRaisesRegex(Unanswered, "^worker worker: guard channel: closed$"):
            channel.redisplay()
        tmux.option.assert_not_called()
        opener.assert_not_called()

    def test_redisplay_without_a_display_opens_nothing(self):
        channel, tmux, _ = self.reopener(terminal=False)
        self.assertFalse(channel.redisplay())
        tmux.option.assert_not_called()
        tmux.start.assert_not_called()

    def test_redisplay_opens_the_display_its_start_opened(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        frames = iter([guard.json_frame(b"s", {"pid": 123})])

        def read(_size):
            try:
                return next(frames)
            except StopIteration:
                gate.wait(GUARD_WAIT)  # running until the test ends
                return b""

        proc = mock.Mock()
        proc.stdout.read.side_effect = read
        tmux = mock.Mock()
        with (
            mock.patch.object(managed.subprocess, "Popen", return_value=proc),
            mock.patch.object(os, "set_blocking"),
        ):
            channel = TmuxHost("tmux", tmux=tmux).worker(
                "worker",
                Launch((sys.executable,), cwd=self.home),
                terminal=True,
                display="manager",
                events="/events",
                metadata={"generation": "a" * 32, "sid": "s1", "client": "claude", "x": "y"},
            )
        first = tmux.start.call_args
        self.assertEqual(
            first.kwargs["tags"],
            {
                "@kind": "guard",
                "@generation": "a" * 32,
                "@sid": "s1",
                "@client": "claude",
                "@display": "manager",
            },
        )
        self.assertEqual(first.kwargs["events"], "/events")
        self.assertEqual(first.args[1].argv[-2], "--attach")
        tmux.option.return_value = None  # its display killed
        self.assertTrue(channel.redisplay())
        self.assertEqual(tmux.start.call_args_list, [first, first])


class OneShotTest(Case):
    """A headless worker's one-shot guard dir: never a tui's, removed by its owner or the sweep."""

    def setUp(self):
        super().setUp()
        self.host = TmuxHost("tmux", tmux=mock.Mock())
        self.root = os.path.dirname(managed.location("tmux", "x"))

    def headless(self, name="x", generation=None):
        proc = mock.Mock()
        proc.stdout.read.side_effect = [
            guard.json_frame(b"s", {"pid": 123}),
            guard.json_frame(b"x", {"returncode": 0}),
            b"",
        ]
        with (
            mock.patch.object(managed.subprocess, "Popen", return_value=proc),
            mock.patch.object(os, "set_blocking"),
        ):
            return self.host.worker(
                name,
                Launch((sys.executable,), cwd=self.home),
                terminal=False,
                metadata={"generation": generation} if generation else None,
            )

    def live_tui(self, name, generation):
        """A tui worker's guard dir whose lease this test holds, as its running guard would."""
        directory = managed.location("tmux", name)
        managed.private_directory(directory + "/" + generation)
        managed.save_json(directory + "/current.json", {"generation": generation})
        managed.save_json(
            directory + "/" + generation + "/state.json",
            {"generation": generation, "state": "running"},
        )
        lease = managed.lease_file(directory + "/" + generation + "/lease")
        managed.fcntl.flock(lease, managed.fcntl.LOCK_EX)
        self.addCleanup(os.close, lease)
        return directory

    def dead_one_shot(self, generation, *, age=120):
        """A one-shot dir whose owner and guard died: both locks free, older than the floor."""
        directory = managed.one_shot("tmux", generation)
        managed.private_directory(directory + "/" + generation)
        os.close(managed.lease_file(directory + "/owner.lock"))
        os.close(managed.lease_file(directory + "/" + generation + "/lease"))
        os.utime(directory, (time.time() - age,) * 2)
        return directory

    def tree(self, directory):
        return sorted(
            os.path.join(base, name)
            for base, dirs, files in os.walk(directory)
            for name in dirs + files
        )

    def test_a_headless_worker_never_touches_a_tui_guard_dir_of_its_name(self):
        tui = self.live_tui("x", "a" * 32)
        before = self.tree(tui)
        worker = self.headless("x")
        self.assertEqual(self.tree(tui), before)
        self.assertEqual(self.host.worker_liveness("x", "a" * 32), Liveness.LIVE)
        self.assertTrue(os.path.isdir(managed.one_shot("tmux", worker.generation)))
        worker.close()
        self.assertEqual(managed.load_json(tui + "/current.json"), {"generation": "a" * 32})
        self.assertEqual(self.tree(tui), before)

    def test_two_headless_workers_of_one_name_run_at_once(self):
        first, second = self.headless("x"), self.headless("x")
        directories = {managed.one_shot("tmux", w.generation) for w in (first, second)}
        self.assertEqual(len(directories), 2)
        self.assertTrue(all(os.path.isdir(directory) for directory in directories))
        first.close()
        second.close()

    def test_a_one_shot_dir_is_named_apart_from_every_tui_dir(self):
        self.assertRegex(os.path.basename(managed.one_shot("tmux", "a" * 32)), r"\Ao[0-9a-f]{15}\Z")
        self.assertRegex(os.path.basename(managed.location("tmux", "x")), r"\A[0-9a-f]{16}\Z")
        self.assertNotEqual(managed.one_shot("tmux", "a" * 32), managed.one_shot("other", "a" * 32))

    def test_a_one_shot_dir_is_removed_at_close(self):
        worker = self.headless("x")
        directory = managed.one_shot("tmux", worker.generation)
        self.assertTrue(os.path.isfile(directory + "/owner.lock"))
        self.assertFalse(os.path.lexists(directory + "/current.json"))
        worker.close()
        self.assertFalse(os.path.lexists(directory))
        worker.close()
        self.host.tmux.option.assert_not_called()  # a headless worker has no display

    def test_a_headless_handle_without_tmux_has_no_display(self):
        channel = managed.TmuxManagedProcess(
            "x", "a" * 32, mock.Mock(), terminal=False, tmux=None, start_reader=False
        )
        channel.close_display()
        self.assertFalse(channel.redisplay())

    def test_a_failed_close_removes_the_dir_only_once_its_guard_is_gone(self):
        for gone in (False, True):
            with self.subTest(gone=gone):
                worker = self.headless("x")
                directory = managed.one_shot("tmux", worker.generation)
                worker.proc.poll.return_value = 0 if gone else None
                worker.fail("guard channel: boom")
                with self.assertRaises(Unanswered):
                    worker.close()
                self.assertEqual(os.path.lexists(directory), not gone)

    def test_a_failed_headless_start_removes_its_one_shot_dir(self):
        with (
            mock.patch.object(managed.subprocess, "Popen", side_effect=FileNotFoundError),
            self.assertRaises(SessionError),
        ):
            self.host.worker(
                "x",
                Launch((sys.executable,), cwd=self.home),
                terminal=False,
                metadata={"generation": "a" * 32},
            )
        self.assertFalse(os.path.lexists(managed.one_shot("tmux", "a" * 32)))

    def test_a_used_generation_is_refused(self):
        self.dead_one_shot("a" * 32, age=0)
        with self.assertRaisesRegex(managed.ConfigError, "worker generation: in use"):
            self.headless("x", "a" * 32)

    def test_the_next_start_removes_a_one_shot_dir_whose_owner_died(self):
        dead = self.dead_one_shot("a" * 32)
        managed.sweep(self.root)
        self.assertFalse(os.path.lexists(dead))
        dead = self.dead_one_shot("b" * 32)
        self.headless("y").close()
        self.assertFalse(os.path.lexists(dead))

    def test_the_sweep_keeps_held_young_and_tui_dirs(self):
        owned = self.dead_one_shot("a" * 32)
        lock = managed.lease_file(owned + "/owner.lock", create=False)
        managed.fcntl.flock(lock, managed.fcntl.LOCK_EX)
        self.addCleanup(os.close, lock)
        guarded = self.dead_one_shot("b" * 32)
        lease = managed.lease_file(guarded + "/" + "b" * 32 + "/lease", create=False)
        managed.fcntl.flock(lease, managed.fcntl.LOCK_EX)
        self.addCleanup(os.close, lease)
        young = self.dead_one_shot("c" * 32, age=0)
        tui = managed.location("tmux", "x")
        managed.private_directory(tui)
        os.utime(tui, (time.time() - 120,) * 2)
        other = self.root + "/o" + "z" * 15
        managed.private_directory(other)
        os.utime(other, (time.time() - 120,) * 2)
        managed.sweep(self.root)
        for directory in (owned, guarded, young, tui, other):
            self.assertTrue(os.path.isdir(directory), directory)

    def test_the_sweep_skips_a_missing_or_foreign_root_and_a_foreign_lock(self):
        managed.sweep(self.root)  # none yet
        self.assertFalse(os.path.lexists(self.root))
        dead = self.dead_one_shot("a" * 32)
        os.chmod(self.root, 0o755)
        with self.assertLogs("session", "WARNING"):
            managed.sweep(self.root)
        self.assertTrue(os.path.isdir(dead))
        os.chmod(self.root, 0o700)
        os.chmod(dead + "/owner.lock", 0o644)
        with self.assertLogs("session", "WARNING"):
            managed.sweep(self.root)
        self.assertTrue(os.path.isdir(dead))


class SocketPathTest(Case):
    """Every guard socket path fits macOS's 104-byte AF_UNIX sun_path: 103 bytes and its NUL."""

    def socket(self, directory, name):
        return os.path.join(directory, "a" * 32, name)

    def test_the_default_root_bounds_every_socket_under_a_long_temp_dir(self):
        with (
            mock.patch.dict(os.environ),
            mock.patch.object(tempfile, "tempdir", "/var/folders/" + "x" * 80 + "/T"),
            mock.patch.object(os, "getuid", return_value=2**32 - 1),
        ):
            del os.environ["ENSEMBLE_CONTROL_ROOT"]
            for directory in (managed.location("tmux", "x"), managed.one_shot("tmux", "a" * 32)):
                for name in ("hook", "display"):
                    path = self.socket(directory, name)
                    self.assertLessEqual(len(os.fsencode(path)), 103, path)

    def test_a_worker_passes_the_check_at_the_limit_and_is_refused_one_byte_over_it(self):
        """Roots sized from `/`, whatever the host's temp dir; a start stops at its first mkdir."""
        host = TmuxHost("tmux", tmux=mock.Mock())
        launch = Launch((sys.executable,), cwd=self.home)
        for terminal, name in ((True, "display"), (False, "hook")):
            with self.subTest(terminal=terminal):
                with mock.patch.dict(os.environ, {"ENSEMBLE_CONTROL_ROOT": "/"}):
                    pad = 103 - len(os.fsencode(self.socket(managed.location("tmux", "x"), name)))
                at, over = "/" + "r" * (pad - 1), "/" + "r" * pad
                for root in (at, over):
                    self.assertFalse(os.path.lexists(root))
                    with (
                        mock.patch.dict(os.environ, {"ENSEMBLE_CONTROL_ROOT": root}),
                        mock.patch.object(os, "mkdir", side_effect=PermissionError) as mkdir,
                        mock.patch.object(managed.subprocess, "Popen") as spawn,
                    ):
                        if root == at:
                            with self.assertRaises(PermissionError):
                                host.worker("worker", launch, terminal=terminal)
                            self.assertEqual(mkdir.call_args.args[0], at)
                        else:
                            with self.assertRaisesRegex(
                                ConfigError, "104 bytes, over the 103-byte"
                            ):
                                host.worker("worker", launch, terminal=terminal)
                            mkdir.assert_not_called()
                        spawn.assert_not_called()
