"""Crew's printed shell commands preserve HOME paths and invalid cursors perform no writes."""

import os
import shlex
import subprocess
from pathlib import Path

from manager.tests import fake_session
from manager.tests.case import Case, child_env


class PathsAndArguments(Case):
    def setUp(self):
        self.command = self.setup_ctl()
        fake_session.install(self.bin_dir, os.path.join(self.home, "session-list"))
        self.crew_bin = self.plugin_root()
        self.env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "ENSEMBLE_MANAGER",
                "ENSEMBLE_HOLDER_ID",
                "ENSEMBLE_HOLDER_GENERATION",
                "ENSEMBLE_HOLDER_PID",
            }
        }

    def invoke(self, *argv, home=None):
        env = dict(self.env)
        if home is not None:
            env["HOME"] = home
        return subprocess.run(
            (self.crew_bin, *argv),
            cwd=self.home,
            env=child_env(env),
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )

    def test_quoted_home_arm_commands_parse_to_exact_paths_and_arguments(self):
        home = Path(self.home, "it's a home")
        home.mkdir()
        started = self.invoke(
            "run",
            "fixture",
            "--manager",
            "mgr",
            "--",
            self.command,
            "start",
            "fixture",
            home=str(home),
        )
        self.assertEqual((started.returncode, started.stderr), (0, ""))
        result = self.invoke("attach", "--manager", "mgr", home=str(home))
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        commands = {
            label: shlex.split(command)
            for line in result.stdout.splitlines()
            for label, separator, command in (line.partition(": "),)
            if separator
        }
        events = home / ".ensemble" / "managers" / "mgr" / "events"
        self.assertEqual(
            commands,
            {
                "monitor": [
                    "ensemble-crew",
                    "next-event",
                    "--manager",
                    "mgr",
                    "--after",
                    "0",
                    "--gen",
                    "0",
                    "--content",
                    "--follow",
                ],
                "monitor expired": [
                    "ensemble-crew",
                    "attach",
                    "--manager",
                    "mgr",
                ],
                "next-event": [
                    "ensemble-crew",
                    "next-event",
                    "--manager",
                    "mgr",
                    "--after",
                    "0",
                    "--gen",
                    "0",
                    "--content",
                ],
                "manual": ["ensemble-crew", "pending", "--manager", "mgr", "--content"],
            },
        )
        self.assertTrue(events.is_file())
        self.assertTrue(events.with_name("roster.json").is_file())
        self.assertTrue(result.stdout.endswith("N=0 GEN=0\n"))

    def test_relative_home_resolves_to_absolute_roster_and_monitor_path(self):
        relative = "relative home"
        home = Path(self.home).resolve() / relative
        home.mkdir()
        started = self.invoke(
            "run",
            "fixture",
            "--manager",
            "mgr",
            "--",
            self.command,
            "start",
            "fixture",
            home=relative,
        )
        self.assertEqual((started.returncode, started.stderr), (0, ""))
        result = self.invoke("attach", "--manager", "mgr", home=relative)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        monitor = next(
            line.partition(": ")[2]
            for line in result.stdout.splitlines()
            if line.startswith("monitor: ")
        )
        events = home / ".ensemble" / "managers" / "mgr" / "events"
        self.assertTrue(events.is_absolute())
        self.assertEqual(
            shlex.split(monitor),
            [
                "ensemble-crew",
                "next-event",
                "--manager",
                "mgr",
                "--after",
                "0",
                "--gen",
                "0",
                "--content",
                "--follow",
            ],
        )
        self.assertTrue(events.is_file())
        self.assertTrue(events.with_name("roster.json").is_file())
        self.assertFalse(Path(self.ensemble, "managers").exists())

    def test_only_ack_accepts_handling_cursors_and_invalid_numbers_write_nothing(self):
        before = {str(path.relative_to(self.home)) for path in Path(self.home).rglob("*")}
        cases = (
            ("attach", ("--after", "3", "--gen", "0"), "unrecognized arguments"),
            ("ack", ("--through", "-1", "--gen", "0"), "want a non-negative integer"),
            ("ack", ("--through", "1", "--gen", "-1"), "want a non-negative integer"),
            ("ack", ("--through", "1", "--gen", "x"), "want a non-negative integer"),
            ("next-event", ("--after", "-1", "--gen", "0"), "want a non-negative integer"),
            ("next-event", ("--after", "0", "--gen", "-1"), "want a non-negative integer"),
            ("next-event", ("--after", "x", "--gen", "0"), "want a non-negative integer"),
            ("next-event", ("--after", "0", "--gen", "x"), "want a non-negative integer"),
        )
        for command, arguments, error in cases:
            with self.subTest(command=command, arguments=arguments):
                result = self.invoke(command, "--manager", "mgr", *arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn(error, result.stderr)
                after = {str(path.relative_to(self.home)) for path in Path(self.home).rglob("*")}
                self.assertEqual(after, before)
                self.assertFalse(Path(self.ensemble, "managers").exists())
