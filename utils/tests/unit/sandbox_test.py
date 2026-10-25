"""Owned tmux fixture cleanup, including failed shutdown and locked allocation, the processes
a supervised run leaves behind, and a tree's project import roots.
"""

import os
import signal
import tempfile
import threading
from pathlib import Path
from unittest import mock

from ensemble.utils.process_control import TIMEOUT

from utils.tests import sandbox
from utils.tests.case import REPO, Case
from utils.tests.process_fixtures import TimeoutExpired


class ProjectRoots(Case):
    def test_only_each_projects_src_holding_just_its_own_subpackage(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for rel in (
                "utils/src/ensemble/utils",
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
            (repo / "utils/src/ensemble/.DS_Store").touch()
            self.assertEqual(
                sandbox.project_roots(tmp), (f"{tmp}/linear-x/src", f"{tmp}/utils/src")
            )

    def test_this_repos_are_its_five_projects(self):
        self.assertEqual(
            sandbox.project_roots(REPO),
            tuple(
                os.path.join(REPO, project, "src")
                for project in ("delegate", "manager", "orchestrator", "session", "utils")
            ),
        )


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
