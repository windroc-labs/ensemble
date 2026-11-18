"""Owned tmux fixture cleanup, including failed shutdown and locked allocation, the processes
a supervised run leaves behind, the sandbox environment and supervised child, and a tree's project
import roots.
"""

import os
import signal
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ensemble.utils.process_control import TIMEOUT
from tests.utils import sandbox
from tests.utils.case import Case
from tests.utils.process_fixtures import TimeoutExpired


class ProjectRoots(Case):
    def test_only_the_shared_src_namespace_is_an_import_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for rel in (
                "src/ensemble/utils",
                "linear-x/src/ensemble/linear_x",
                "tmpq1/src/ensemble/utils",
                "two/src/ensemble/two",
                "two/src/ensemble/other",
                "inited/src/ensemble/inited",
                "utils/nested/src/ensemble/nested",
                "bare/src/ensemble/bare",
            ):
                (repo / rel).mkdir(parents=True)
                if rel != "bare/src/ensemble/bare":
                    (repo / rel / "__init__.py").touch()
            (repo / "inited/src/ensemble/__init__.py").touch()
            (repo / "src/ensemble/.DS_Store").touch()
            self.assertEqual(sandbox.project_roots(tmp), (f"{tmp}/src",))


class TmuxCleanup(Case):
    def sockets(self, *names):
        root = Path(self.home, "owned")
        socket_directory = root / f"tmux-{os.getuid()}"
        socket_directory.mkdir(parents=True)
        for name in names:
            (socket_directory / name).touch()
        return root, [str(socket_directory / name) for name in names]

    def test_shutdown_failures_still_remove_the_owned_directory(self):
        for error in (FileNotFoundError("tmux"), TimeoutExpired("tmux", 10)):
            with self.subTest(error=type(error).__name__):
                root, _ = self.sockets("default")
                with (
                    mock.patch("pathlib.Path.is_socket", return_value=True),
                    mock.patch("subprocess.run", side_effect=error) as run,
                    self.assertRaises(ExceptionGroup) as raised,
                ):
                    sandbox._remove_tmux_directory(os.getpid(), str(root))
                self.assertEqual(raised.exception.exceptions, (error,))
                run.assert_called_once()
                self.assertFalse(root.exists())

    def test_a_failed_socket_does_not_stop_the_other_servers(self):
        root, sockets = self.sockets("first", "second", "third")
        failure = TimeoutExpired("tmux", 10)
        with (
            mock.patch("pathlib.Path.is_socket", return_value=True),
            mock.patch(
                "subprocess.run",
                side_effect=[failure, mock.Mock(returncode=0), mock.Mock(returncode=1)],
            ) as run,
            self.assertRaises(ExceptionGroup) as raised,
        ):
            sandbox.stop_tmux_servers(str(root), {})
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [["tmux", "-S", socket, "kill-server"] for socket in sockets],
        )
        self.assertEqual(raised.exception.exceptions, (failure,))

    def test_cleanup_does_not_acquire_the_allocation_lock(self):
        root, _ = self.sockets("default")
        with (
            mock.patch("pathlib.Path.is_socket", return_value=True),
            mock.patch("subprocess.run") as run,
        ):
            with sandbox._TMUX_LOCK:
                cleanup = threading.Thread(
                    target=sandbox._remove_tmux_directory,
                    args=(os.getpid(), str(root)),
                    daemon=True,
                )
                cleanup.start()
                cleanup.join(timeout=10)
                blocked = cleanup.is_alive()
            cleanup.join()
        self.assertFalse(blocked, "tmux cleanup waited for the allocation lock")
        self.assertEqual(run.call_args.kwargs["env"], {"PATH": os.environ["PATH"]})
        self.assertFalse(root.exists())


class ControlRoot(Case):
    def test_clean_env_carries_the_control_root_into_a_base_without_one(self):
        with mock.patch.dict(os.environ, {"ENSEMBLE_CONTROL_ROOT": "/sandbox"}):
            self.assertEqual(sandbox.clean_env({})["ENSEMBLE_CONTROL_ROOT"], "/sandbox")
            own = sandbox.clean_env({"ENSEMBLE_CONTROL_ROOT": "/own"})
            self.assertEqual(own["ENSEMBLE_CONTROL_ROOT"], "/own")
            del os.environ["ENSEMBLE_CONTROL_ROOT"]
            self.assertNotIn("ENSEMBLE_CONTROL_ROOT", sandbox.clean_env({}))


class AgentSession(Case):
    def test_clean_env_drops_the_agent_session_marker_so_suites_run_inside_one(self):
        env = sandbox.clean_env({"ENSEMBLE_AGENT_SESSION": "1", "KEEP": "k"})
        self.assertNotIn("ENSEMBLE_AGENT_SESSION", env)
        self.assertEqual(env["KEEP"], "k")


PS = """\
  100     1   100   501 Ss   /usr/bin/python3 run_tests.py Unit
  200   100   100   501 S    /usr/bin/python3 -P -S run_tests.py Unit
  300     1   300   501 Ss   python3 -I /x/process_control.py /sb/tmp/apm-group-1
  301   300   301   501 S    python3 -I /x/process_control.py /sb/tmp/apm-group-1
  302   301   301   501 S    cat -
  400     1   400   501 S    sleep 60 /ws/marker
  500     1   500     0 S    sleep 60 /sb/tmp/root-owned
  600     1   600   501 Z    sleep 60 /sb/tmp/zombie
  700     1   700   501 S    sleep 60 /elsewhere
  800   200   100   501 S    python3 /sb/tmp/fixture.py
"""


class Leaks(Case):
    def listing(self):
        return mock.patch(
            "subprocess.run", return_value=mock.Mock(returncode=0, stdout=PS, stderr="")
        )

    def test_leaks_are_this_users_live_processes_naming_a_sandbox_path(self):
        with (
            self.listing(),
            mock.patch("os.getuid", return_value=501),
            mock.patch("os.getpid", return_value=200),
        ):
            rows = sandbox.leaked_processes(["/sb/tmp", "/ws"], grace=0)
        self.assertEqual(
            rows,
            [
                (300, 1, 300, "python3 -I /x/process_control.py /sb/tmp/apm-group-1"),
                (301, 300, 301, "python3 -I /x/process_control.py /sb/tmp/apm-group-1"),
                (400, 1, 400, "sleep 60 /ws/marker"),
                (800, 200, 100, "python3 /sb/tmp/fixture.py"),
            ],
        )

    def test_the_runner_and_its_ancestors_are_never_leaks(self):
        with (
            self.listing(),
            mock.patch("os.getuid", return_value=501),
            mock.patch("os.getpid", return_value=800),
        ):
            rows = sandbox.leaked_processes(["/sb/tmp"], grace=0)
        self.assertEqual([row[0] for row in rows], [300, 301])

    def test_a_leak_loses_its_group_and_the_groups_its_children_lead(self):
        leaked = [
            (300, 1, 300, "python3 -I /x/process_control.py /sb/tmp/apm-group-1"),
            (800, 200, 100, "python3 /sb/tmp/fixture.py"),
        ]
        with (
            self.listing(),
            mock.patch("os.getuid", return_value=501),
            mock.patch("os.killpg") as killpg,
            mock.patch("os.kill") as kill,
        ):
            sandbox.kill_processes(leaked)
        self.assertEqual(
            sorted(killpg.call_args_list),
            [mock.call(300, signal.SIGKILL), mock.call(301, signal.SIGKILL)],
        )
        kill.assert_called_once_with(800, signal.SIGKILL)

    def test_stopped_servers_are_the_ones_that_answered(self):
        root = Path(self.home, "owned")
        directory = root / f"tmux-{os.getuid()}"
        directory.mkdir(parents=True)
        for name in ("dead", "live"):
            (directory / name).touch()
        with (
            mock.patch("pathlib.Path.is_socket", return_value=True),
            mock.patch(
                "subprocess.run", side_effect=[mock.Mock(returncode=1), mock.Mock(returncode=0)]
            ),
        ):
            self.assertEqual(sandbox.stop_tmux_servers(str(root), {}), [str(directory / "live")])

    def test_the_grace_outlasts_a_controller_waiting_for_its_client(self):
        self.assertGreaterEqual(sandbox.CONTROL_GRACE, TIMEOUT + 2)


class Probes(Case):
    def test_a_pid_exists_unless_signal_0_finds_no_process(self):
        with mock.patch("os.kill") as kill:
            self.assertTrue(sandbox.pid_exists(7))
        kill.assert_called_once_with(7, 0)
        with mock.patch("os.kill", side_effect=ProcessLookupError):
            self.assertFalse(sandbox.pid_exists(7))
        with mock.patch("os.kill", side_effect=PermissionError), self.assertRaises(PermissionError):
            sandbox.pid_exists(7)

    def test_a_pid_runs_while_ps_shows_it_in_a_state_other_than_zombie(self):
        for stat, running in (("S+\n", True), ("Z+\n", False), ("", False)):
            with (
                self.subTest(stat=stat),
                mock.patch("subprocess.run", return_value=mock.Mock(stdout=stat)) as ps,
            ):
                self.assertEqual((sandbox.running(7), sandbox.gone(7, 5)), (running, not running))
            self.assertEqual(ps.call_args_list[0].args, (["ps", "-o", "stat=", "-p", "7"],))
            self.assertEqual(
                ps.call_args_list[0].kwargs["env"], {"PATH": os.defpath, "LC_ALL": "C"}
            )
            self.assertEqual([c.kwargs["timeout"] for c in ps.call_args_list], [60, 5])

    def test_kill_sends_sigkill_and_ignores_a_gone_or_foreign_pid(self):
        for error in (None, ProcessLookupError, PermissionError):
            with self.subTest(error=error), mock.patch("os.kill", side_effect=error) as kill:
                self.assertIsNone(sandbox.kill(7))
            kill.assert_called_once_with(7, signal.SIGKILL)


class Scratch(Case):
    def test_a_scratch_dir_is_real_or_raw_under_its_parent_until_cleanup(self):
        link = os.path.join(self.home, "link")
        os.symlink(self.home, link)
        owner = unittest.TestCase()
        real, raw = sandbox.scratch(owner, link), sandbox.scratch(owner, link, real=False)
        default = sandbox.scratch(owner)
        self.assertEqual(os.path.dirname(raw), link)
        self.assertEqual(os.path.dirname(real), os.path.realpath(self.home))
        self.assertEqual(os.path.dirname(default), os.path.realpath(tempfile.gettempdir()))
        self.assertTrue(all(map(os.path.isdir, (real, raw, default))))
        owner.doCleanups()
        self.assertFalse(any(map(os.path.exists, (real, raw, default))))


class SandboxEnv(Case):
    def test_the_sandbox_replaces_whatever_would_steer_a_test(self):
        caller = {
            "PATH": "/bin",
            "KEPT": "1",
            "HOME": "/real/home",
            "ENSEMBLE_CLIENT": "claude",
            "ENSEMBLE_GUARD_HOOK_SOCKET": "/run/ensemble/guard/ci/hook",
            "GIT_DIR": "elsewhere",
            "TMUX": "/tmp/tmux-1/default,1,0",
            "PYTHONPATH": "/other/checkout/src",
        }
        env = sandbox.sandbox_env(caller, "/sb/tmp", "/repo/.test-sandbox-1", "/repo", "/repo/src")
        self.assertEqual(
            env,
            {
                "PATH": "/bin",
                "KEPT": "1",
                "HOME": "/repo/.test-sandbox-1",
                "TMPDIR": "/sb/tmp",
                "TMUX_TMPDIR": "/sb/tmp",
                "ENSEMBLE_TEST_SANDBOX": "/sb/tmp",
                "ENSEMBLE_CONTROL_ROOT": "/sb/tmp",
                "ENSEMBLE_TEST_TMP": "/repo/.test-sandbox-1",
                "ENSEMBLE_TEST_WORKSPACE": "/repo/.test-sandbox-1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CEILING_DIRECTORIES": "/repo",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": os.pathsep.join(("/repo", "/repo/src")),
            },
        )


class Supervise(Case):
    def supervised(self, code, *, servers=(), leaked=()):
        """((exit code, leaks), what the callbacks saw) of one supervised run."""
        seen = {}

        def run(env, tmp):
            seen.update(env=env, tmp=tmp, during=tmp.is_dir() and os.path.isdir(env["HOME"]))
            return code

        with (
            mock.patch.object(sandbox, "stop_tmux_servers", return_value=list(servers)) as stop,
            mock.patch.object(sandbox, "leaked_processes", return_value=list(leaked)) as leaks,
            mock.patch.object(sandbox, "kill_processes") as kill,
        ):
            result = sandbox.supervise(
                run,
                Path(self.home),
                prepare=lambda tmp: seen.update(prepared=tmp, prepared_exists=tmp.is_dir()),
                collect=lambda tmp: seen.update(collected=tmp, collected_exists=tmp.is_dir()),
                grace=3,
            )
        seen.update(stop=stop, leaks=leaks, kill=kill)
        return result, seen

    def test_the_child_runs_in_the_sandbox_and_its_code_comes_back(self):
        result, seen = self.supervised(2)
        env, tmp = seen["env"], seen["tmp"]
        workspace = env["ENSEMBLE_TEST_WORKSPACE"]
        self.assertEqual(result, (2, []))
        self.assertEqual(
            (env["HOME"], env["TMPDIR"], env["ENSEMBLE_TEST_SANDBOX"]),
            (workspace, str(tmp), str(tmp)),
        )
        self.assertEqual(os.path.dirname(workspace), self.home)
        self.assertTrue(os.path.basename(workspace).startswith(".test-sandbox-"))
        self.assertEqual(
            env["PYTHONPATH"].split(os.pathsep)[:2], [self.home, os.path.join(self.home, ".github")]
        )
        self.assertEqual((seen["prepared"], seen["collected"]), (tmp, tmp))
        self.assertTrue(seen["prepared_exists"] and seen["collected_exists"] and seen["during"])
        self.assertFalse(tmp.exists() or os.path.exists(workspace))
        seen["stop"].assert_called_once_with(str(tmp), env)
        seen["leaks"].assert_called_once_with((str(tmp), workspace), grace=3)
        seen["kill"].assert_called_once_with([])

    def test_a_child_killed_by_a_signal_exits_128_plus_the_signal(self):
        self.assertEqual(self.supervised(-signal.SIGKILL)[0], (128 + signal.SIGKILL, []))

    def test_what_the_child_left_behind_is_killed_and_named(self):
        leaked = [(400, 1, 400, "sleep 60 /ws/marker")]
        result, seen = self.supervised(0, servers=["/sb/tmux-501/default"], leaked=leaked)
        self.assertEqual(
            result, (0, ["tmux server /sb/tmux-501/default", "pid 400: sleep 60 /ws/marker"])
        )
        seen["kill"].assert_called_once_with(leaked)

    def test_the_sandbox_is_stopped_and_removed_when_the_child_cannot_start(self):
        def run(_env, tmp):
            seen.update(tmp=tmp)
            raise OSError("no python")

        seen = {}
        with (
            mock.patch.object(sandbox, "stop_tmux_servers", return_value=[]) as stop,
            self.assertRaises(OSError),
        ):
            sandbox.supervise(
                run, Path(self.home), prepare=lambda _tmp: None, collect=lambda _tmp: None
            )
        stop.assert_called_once()
        self.assertFalse(seen["tmp"].exists())


class RunnerSandbox(Case):
    def _case_env(self):
        return {}

    def test_the_runner_gives_each_test_the_sandbox(self):
        env = os.environ
        tmp, workspace = env["ENSEMBLE_TEST_SANDBOX"], env["ENSEMBLE_TEST_WORKSPACE"]
        repo = str(Path(__file__).resolve().parents[3])
        self.assertEqual((env["HOME"], env["ENSEMBLE_TEST_TMP"]), (workspace, workspace))
        self.assertEqual(
            (env["TMPDIR"], env["ENSEMBLE_CONTROL_ROOT"], tempfile.gettempdir()), (tmp, tmp, tmp)
        )
        self.assertTrue(os.path.isdir(tmp) and os.path.isdir(workspace))
        self.assertEqual(os.path.dirname(workspace), repo)
        self.assertTrue(os.path.basename(workspace).startswith(".test-sandbox-"))
        self.assertTrue(Path(env["TMUX_TMPDIR"]).is_relative_to(tmp))
        self.assertEqual(
            [key for key in env if key.startswith("GIT_") or key in sandbox.CALLER_SESSION]
            + [key for key in ("ENSEMBLE_CLIENT", "ENSEMBLE_GUARD_HOOK_SOCKET") if key in env],
            ["GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_CEILING_DIRECTORIES"],
        )
        self.assertEqual((env["GIT_CONFIG_GLOBAL"], env["GIT_CONFIG_NOSYSTEM"]), (os.devnull, "1"))
        roots = env["PYTHONPATH"].split(os.pathsep)
        self.assertEqual(
            roots[:3], [repo, os.path.join(repo, ".github"), os.path.join(repo, "src")]
        )
