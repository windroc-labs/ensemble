"""tests/session/live_tmux.py against a real tmux server: its env, control-mode client, readers,
cleanup, grid assertion, wait, and the skip without tmux.
"""

import os
import shutil
import stat
import subprocess
import sys
import unittest
from unittest import mock

from tests.session import live_tmux
from tests.session.case import PLUGIN_BIN, Case

INHERITED = "import subprocess, sys; raise SystemExit(subprocess.call([sys.argv[1], 'ls', '-F', '#{session_name}']))"
EXPLICIT = "import subprocess, sys; raise SystemExit(subprocess.call([sys.argv[1], '-S', sys.argv[2], 'ls', '-F', '#{session_name}'], env={}))"
DROPPED = (
    "import os, subprocess, sys; env = dict(os.environ); env.pop('TMUX_TMPDIR'); "
    "raise SystemExit(subprocess.call([sys.argv[1], 'ls'], env=env))"
)
REPLACED = (
    "import os, subprocess, sys; env = dict(os.environ, TMUX_TMPDIR=sys.argv[2]); "
    "raise SystemExit(subprocess.call([sys.argv[1], 'ls'], env=env))"
)


class Live(Case):
    def setUp(self):
        self.server = live_tmux.Server(self, self.home)

    def ok(self, *args) -> str:
        res = self.server.tmux(*args)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout.strip()


class NoTmux(Case):
    def test_skips_before_making_anything(self):
        with (
            mock.patch.object(live_tmux.shutil, "which", return_value=None),
            mock.patch.object(live_tmux.tempfile, "mkdtemp") as mkdtemp,
            self.assertRaises(unittest.SkipTest) as cm,
        ):
            live_tmux.Server(self, self.home)
        self.assertEqual(str(cm.exception), "tmux not installed")
        mkdtemp.assert_not_called()


class Env(Live):
    def test_env_is_built_from_scratch(self):
        leaks = {
            "TMUX": "/nonexistent/default,1,0",
            "TMUX_PANE": "%0",
            "ITERM_SESSION_ID": "w0t0p0:X",
            "TERM_PROGRAM": "iTerm.app",
        }
        with mock.patch.dict(os.environ, leaks):
            env = self.server.env(EXTRA="x")
        root = self.server.root
        tmux = os.path.dirname(shutil.which("tmux"))
        self.assertEqual(
            env,
            {
                "PATH": os.pathsep.join(
                    (
                        f"{root}/bin",
                        PLUGIN_BIN,
                        os.path.dirname(sys.executable),
                        tmux,
                        "/usr/bin",
                        "/bin",
                    )
                ),
                "HOME": self.home,
                "PYTHONUTF8": "1",
                "TMUX_TMPDIR": root,
                "ENSEMBLE_CONTROL_ROOT": os.environ["ENSEMBLE_CONTROL_ROOT"],
                "EXTRA": "x",
            },
        )
        self.assertEqual(
            shutil.which("claude", path=env["PATH"]), os.path.join(root, "bin", "claude")
        )

    def test_tmpdir_and_socket_reach_one_server(self):
        sock = self.server.socket
        self.assertEqual(sock, os.path.join(self.server.root, f"tmux-{os.getuid()}", "default"))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(sock)).st_mode), 0o700)
        self.server.start("mgr")
        res = subprocess.run(
            ["tmux", "ls", "-F", "#{session_name}"],
            env=self.server.env(),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
        )
        self.assertEqual((res.returncode, res.stdout), (0, "mgr\n"), res.stderr)
        self.assertEqual(
            self.ok(
                "display-message",
                "-p",
                "-t",
                "=mgr:",
                "#{socket_path} #{window_width}x#{window_height} #{pane_current_command}",
            ),
            f"{sock} 200x50 cat",
        )

    def test_inside_runs_a_process_in_the_session_pane(self):
        self.server.start("mgr")
        inside = self.server.inside("mgr")
        pid, pane = self.ok("display-message", "-p", "-t", "=mgr:", "#{pid} #{pane_id}").split()
        self.assertEqual(inside, {"TMUX": f"{self.server.socket},{pid},0", "TMUX_PANE": pane})
        res = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{session_name}"],
            env=self.server.env(**inside),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
        )
        self.assertEqual(res.stdout, "mgr\n", res.stderr)


class FixtureGuard(Live):
    def test_parent_calls_require_a_registered_fixture_socket(self):
        wrong = self.home
        for argv, env in (
            (["tmux", "ls"], None),
            (["tmux", "ls"], self.server.env(TMUX_TMPDIR=wrong)),
            (["tmux", "ls"], self.server.env(TMUX=wrong + "/socket,1,0")),
            (["tmux", "-S", wrong + "/socket", "ls"], self.server.env()),
            (["tmux", "-S"], self.server.env()),
            (["tmux", "-S" + self.server.socket, "ls"], self.server.env()),
            (["tmux", "-Lother", "ls"], self.server.env()),
        ):
            with (
                self.subTest(argv=argv, env=env),
                self.assertRaisesRegex(RuntimeError, "live_tmux"),
            ):
                subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)

    def test_explicit_socket_needs_no_inherited_environment(self):
        self.server.start("mgr")
        result = subprocess.run(
            [self.server.program, "-S", self.server.socket, "ls", "-F", "#{session_name}"],
            env={},
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual((result.returncode, result.stdout), (0, "mgr\n"), result.stderr)

    def test_multiple_registered_servers_remain_independent(self):
        with mock.patch.dict(os.environ, self.server.env(), clear=True):
            other = live_tmux.Server(self, self.home)
        self.server.start("first")
        other.start("second")
        self.assertEqual(self.ok("ls", "-F", "#{session_name}"), "first")
        self.assertEqual(other.tmux("ls", "-F", "#{session_name}").stdout, "second\n")

    def child(self, argv):
        self.assertEqual(self.server.program, os.path.join(self.server.bin, "tmux"))
        return subprocess.run(
            argv,
            env=self.server.env(),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_nested_python_inherits_the_fixture_server_or_uses_an_explicit_socket(self):
        self.server.start("mgr")
        for case, argv in (
            (
                "inherited",
                [sys.executable, "-I", "-c", INHERITED, self.server.program],
            ),
            (
                "explicit socket, no inherited environment",
                [sys.executable, "-I", "-c", EXPLICIT, self.server.program, self.server.socket],
            ),
        ):
            with self.subTest(case=case):
                result = self.child(argv)
                self.assertEqual((result.returncode, result.stdout), (0, "mgr\n"), result.stderr)

    def test_nested_python_cannot_drop_or_replace_the_private_selector(self):
        for case, argv in (
            ("dropped", [sys.executable, "-I", "-c", DROPPED, self.server.program]),
            (
                "replaced",
                [sys.executable, "-I", "-c", REPLACED, self.server.program, self.home],
            ),
        ):
            with self.subTest(case=case):
                result = self.child(argv)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("live_tmux", result.stderr)


class Client(Live):
    def test_attach_shows_the_session_in_a_control_mode_client(self):
        self.server.start("mgr")
        self.server.attach("mgr")
        (row,) = self.ok(
            "list-clients", "-t", "=mgr", "-F", "#{client_control_mode} #{client_tty}"
        ).splitlines()
        control, tty = row.split(" ", 1)
        self.assertEqual(control, "1")
        self.assertTrue(tty.startswith("/dev/"), tty)
        self.assertEqual(
            self.ok("display-message", "-p", "-t", "=mgr:", "#{window_width}x#{window_height}"),
            "200x50",
        )

    def test_attach_resends_a_lost_resize(self):
        self.server.start("mgr")
        self.ok("new-session", "-d", "-s", "w1", "-x", "80", "-y", "24", *live_tmux.IDLE)
        write, dropped = os.write, []

        def lossy(fd: int, data: bytes) -> int:
            if data.startswith(b"refresh-client") and not dropped:
                dropped.append(data)
                return len(data)
            return write(fd, data)

        with mock.patch("os.write", side_effect=lossy):
            self.server.attach("w1")
        self.assertEqual(dropped, [b"refresh-client -C 200x50\n"])
        self.assertEqual(
            self.ok("display-message", "-p", "-t", "=w1:", "#{window_width}x#{window_height}"),
            "200x50",
        )


class Readers(Live):
    def test_option_pane_of_panes_events(self):
        self.server.start("mgr")
        pane = self.ok("display-message", "-p", "-t", "=mgr:", "#{pane_id}")
        self.assertEqual(self.server.panes("=mgr:"), {pane: (0, 0, 200, 50)})
        self.assertEqual(
            (self.server.option("mgr", "@state"), self.server.pane_of("mgr")), ("", "")
        )
        self.ok("set-option", "-t", "=mgr:", "@state", "done")
        self.ok("set-option", "-t", "=mgr:", "@pane", pane)
        self.assertEqual(
            (self.server.option("mgr", "@state"), self.server.pane_of("mgr")), ("done", pane)
        )
        path = os.path.join(self.server.root, "events")
        self.assertEqual(live_tmux.events(path), [])
        with open(path, "w") as f:
            f.write("12:00:00 w1 done\n12:00:01 w1 dead\n")
        self.assertEqual(live_tmux.events(path), ["12:00:00 w1 done", "12:00:01 w1 dead"])


class Cleanup(Case):
    def test_cleanups_kill_the_server_and_client_and_remove_the_root(self):
        seen = {}

        def body():
            server = live_tmux.Server(nested, self.home)
            server.start("mgr")
            server.attach("mgr")
            seen.update(server=server, pid=int(server.inside("mgr")["TMUX"].split(",")[1]))

        nested = unittest.FunctionTestCase(body)
        result = unittest.TestResult()
        nested.run(result)
        self.assertEqual(result.errors + result.failures, [])
        if result.skipped:
            self.skipTest(result.skipped[0][1])
        server = seen["server"]
        self.assertFalse(os.path.exists(server.root))
        with self.assertRaises(ProcessLookupError):
            os.kill(seen["pid"], 0)
        (client,) = server.clients
        self.assertIsNotNone(client.proc.returncode)
        self.assertFalse(client.thread.is_alive())
        with self.assertRaises(OSError):
            os.fstat(client.master)


class Grid(Live):
    def split(self, pane: str, flag: str, size: int) -> str:
        """The new pane, `size` wide (-h, right of `pane`) or high (-v, below)."""
        return self.ok(
            "split-window",
            "-d",
            flag,
            "-l",
            str(size),
            "-t",
            pane,
            "-P",
            "-F",
            "#{pane_id}",
            *live_tmux.IDLE,
        )

    def test_assert_grid_on_a_hand_built_window(self):
        self.server.start("mgr")
        manager = self.ok("display-message", "-p", "-t", "=mgr:", "#{pane_id}")
        self.ok("set-option", "-w", "-t", "=mgr:", "@grid-manager", manager)
        # 200x50: manager 99 wide; column 1 (x 100, 50 wide): w1 over s1, s2; column 2 (x 151, 49
        # wide): p, z1 over z2
        right = self.split(manager, "-h", 100)
        col2 = self.split(right, "-h", 49)
        group = self.split(right, "-v", 25)
        panes = {"w1": right, "s1": group, "s2": self.split(group, "-v", 12), "p": col2}
        panes["z1"] = self.split(col2, "-h", 24)
        panes["z2"] = self.split(panes["z1"], "-v", 25)
        for name, pane in panes.items():
            self.ok("new-session", "-d", "-s", name, *live_tmux.IDLE)
            self.ok("set-option", "-t", f"={name}:", "@pane", pane)
        grid = [["w1", ["s1", "s2"]], [("p", ["z1", "z2"])]]
        self.assertTrue(live_tmux.assert_grid(self, self.server, "mgr", grid))
        self.assertTrue(live_tmux.assert_grid(self, self.server, "mgr", grid, manager_width=99))
        wrong = {
            "column order": ([grid[1], grid[0]], None),
            "cell order": ([[["s1", "s2"], "w1"], grid[1]], None),
            "orphan order": ([["w1", ["s2", "s1"]], grid[1]], None),
            "sub order": ([grid[0], [("p", ["z2", "z1"])]], None),
            "other panes": ([grid[0], ["p"]], None),
            "manager width": (grid, 100),
        }
        for why, (columns, width) in wrong.items():
            with self.subTest(why), self.assertRaises(AssertionError):
                live_tmux.assert_grid(self, self.server, "mgr", columns, manager_width=width)


class Wait(Case):
    def test_returns_the_truthy_value(self):
        values = iter([0, "", None, "yes"])
        self.assertEqual(live_tmux.wait(lambda: next(values)), "yes")

    def test_a_failed_assertion_counts_as_falsy(self):
        values = iter([AssertionError("not yet"), 7])

        def check():
            value = next(values)
            if isinstance(value, AssertionError):
                raise value
            return value

        self.assertEqual(live_tmux.wait(check), 7)

    def test_times_out_with_what_and_the_last_value(self):
        with self.assertRaises(AssertionError) as cm:
            live_tmux.wait(lambda: [], timeout=0.2, what="the moon to rise")
        self.assertIn("the moon to rise", str(cm.exception))
        self.assertIn("[]", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
