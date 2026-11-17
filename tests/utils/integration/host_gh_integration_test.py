"""The host gh wrapper runs a real gh child: its exit, signals, stdio and token refreshes beside it."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time

from tests.utils.case import REPO, Case

FIXTURES = os.path.join(REPO, "tests", "utils", "integration", "host_gh_fixtures")
SCRIPT = os.path.join(REPO, "src", "ensemble", "utils", "host_gh.py")


class HostGhChild(Case):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", self.root], check=False)
        self.state = os.path.join(self.root, "host-gh")
        os.mkdir(self.state, 0o700)
        stamp = os.path.join(self.state, "refreshed")
        with open(stamp, "w", encoding="utf-8"):
            pass
        bin_dir = os.path.join(self.root, "bin")
        os.mkdir(bin_dir)
        os.symlink(os.path.join(FIXTURES, "fake_gh.py"), os.path.join(bin_dir, "gh"))
        self.gh_log = os.path.join(self.root, "gh.log")
        self.broker_log = os.path.join(self.root, "broker.log")
        self.env = {
            "HOME": self.root,
            "PATH": os.pathsep.join([bin_dir, "/usr/bin", "/bin"]),
            "FAKE_GH_LOG": self.gh_log,
            "FAKE_BROKER_LOG": self.broker_log,
        }

    def start(self, *argv, **env):
        return subprocess.Popen(
            [
                sys.executable,
                "-I",
                os.path.join(FIXTURES, "drive.py"),
                SCRIPT,
                self.state,
                os.path.join(self.root, "ensemble-bin", "gh"),
                os.path.join(FIXTURES, "fake_broker.py"),
                *argv,
            ],
            env={**self.env, **env},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def notes(self, path):
        try:
            with open(path, encoding="utf-8") as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def wait_ready(self):
        deadline = time.monotonic() + 20
        while not any(n.get("ready") for n in self.notes(self.gh_log)):
            self.assertLess(time.monotonic(), deadline, "gh never started")
            time.sleep(0.05)

    def test_the_wrapper_exits_and_reads_and_writes_as_its_gh_child(self):
        for mode, stdin, code, out in (("exit:3", "", 3, ""), ("echo", "hi\n", 0, "hi\n")):
            with self.subTest(mode):
                proc = self.start("pr", "view", FAKE_GH_MODE=mode)
                got, err = proc.communicate(stdin, timeout=30)
                self.assertEqual((proc.returncode, got), (code, out), err)
        run = self.notes(self.gh_log)[-1]
        self.assertEqual(run["argv"], ["pr", "view"])
        self.assertEqual(self.notes(self.broker_log), [])

    def test_a_child_killed_by_a_signal_ends_the_wrapper_by_it(self):
        proc = self.start("api", "x", FAKE_GH_MODE=f"die:{int(signal.SIGTERM)}")
        _, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, -signal.SIGTERM, err)

    def test_sigterm_is_forwarded_once_and_the_terminals_signals_are_left_to_the_group(self):
        proc = self.start("run", "watch", "7", FAKE_GH_MODE="wait:2")
        self.wait_ready()
        os.kill(proc.pid, signal.SIGINT)
        os.kill(proc.pid, signal.SIGTERM)
        _, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 5, err)
        notes = self.notes(self.gh_log)
        self.assertEqual(notes[-1]["signals"], [signal.SIGTERM])
        self.assertEqual((notes[0]["int_ignored"], notes[0]["quit_ignored"]), (False, False))

    def test_a_signal_another_thread_gets_is_still_forwarded_once(self):
        proc = self.start(
            "run", "watch", FAKE_GH_MODE="wait:2", DRIVE_OFF_MAIN_SIGNAL=str(int(signal.SIGTERM))
        )
        _, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 5, err)
        self.assertEqual(self.notes(self.gh_log)[-1]["signals"], [signal.SIGTERM])

    def test_the_token_is_refreshed_beside_a_running_child_never_restarting_it(self):
        proc = self.start(
            "pr",
            "checks",
            "--watch",
            FAKE_GH_MODE="wait:1.5",
            HOST_GH_CHECK_EVERY="0.1",
            HOST_GH_REFRESH_AFTER="0.5",
        )
        _, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 5, err)
        calls = self.notes(self.broker_log)
        self.assertGreaterEqual(len(calls), 1, err)
        self.assertEqual(calls[0][:2], ["host-gh-login", "--gh"])
        runs = [n for n in self.notes(self.gh_log) if "argv" in n]
        self.assertEqual([n["argv"] for n in runs], [["pr", "checks", "--watch"]])
