import os
import shlex
import subprocess
import sys
from pathlib import Path

from tests.case import PY, REPO, Case, child_env

SURFACES = {
    "crew": (
        "manager",
        [
            "hold",
            "run",
            "worker",
            "issue",
            "role",
            "attach",
            "status",
            "report",
            "resume",
            "hide",
            "show",
            "close",
            "forget",
            "close-all",
            "note",
            "release",
            "next-event",
            "resume-commander",
        ],
    ),
    "session": (
        "session",
        [
            "start",
            "restart",
            "resume",
            "reply",
            "status",
            "content",
            "show",
            "hide",
            "close",
            "ls",
            "send",
            "read",
            "tile",
            "hook",
            "client-resume",
            "resume-argv",
        ],
    ),
    "delegate": (
        "delegate",
        [
            "run",
            "open",
            "status",
            "content",
            "outcome",
            "resume",
            "show",
            "hide",
            "close",
            "prompt",
        ],
    ),
    "orchestrator": (
        "orchestrator",
        [
            "router",
            "promote",
            "prune",
            "issue",
            "status",
            "show",
            "hide",
            "close",
            "resume",
            "content",
            "writeback",
        ],
    ),
}


class HelpSafety(Case):
    def setUp(self):
        self.probe = Path(REPO, "tests", "help_probe.py")
        tools = Path(self.home, "tools")
        tools.mkdir()
        python = tools / "python3"
        python.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -P {shlex.quote(str(self.probe))} "$@"\n'
        )
        python.chmod(0o755)
        self.env = child_env(
            {
                "HOME": self.home,
                "PATH": f"{tools}:/usr/bin:/bin",
                "ENSEMBLE_AGENT_SESSION": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "COLUMNS": "80",
            }
        )
        installer = Path(REPO, "skills", "manage", "scripts", "ensemble")
        installed = subprocess.run(
            [str(installer), "--install"],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual((installed.returncode, installed.stderr), (0, ""))
        cache = Path(self.home, ".codex", "plugins", "cache", "windroc", "ensemble")
        cache.mkdir(parents=True)
        (cache / "9.9.9").symlink_to(REPO, target_is_directory=True)

    def snapshot(self):
        return {
            str(path.relative_to(self.home)): ("link", os.readlink(path))
            if path.is_symlink()
            else ("file", path.read_bytes())
            if path.is_file()
            else ("dir",)
            for path in Path(self.home).rglob("*")
        }

    def help_only(self, command):
        with subprocess.Popen(
            command,
            cwd=self.home,
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                self.fail(f"help waited with stdin held open: {command}")
            out, err = process.communicate()
            self.assertEqual((process.returncode, err), (0, ""), command)
            self.assertIn("ensemble-", out)

    def matrix(self, root):
        self.assertEqual(sum(len(commands) + 1 for _, commands in SURFACES.values()), 59)
        before = self.snapshot()
        package, commands = SURFACES[root]
        for leaf in [None, *commands]:
            for flag in ("--help", "-h"):
                arguments = ([] if leaf is None else [leaf]) + [flag]
                entries = ("module", "repository", "installed")
                for entry in entries if leaf in (None, commands[0]) else entries[:1]:
                    with self.subTest(root=root, leaf=leaf, flag=flag, entry=entry):
                        if entry == "module":
                            command = [
                                *PY,
                                str(self.probe),
                                "-m",
                                f"ensemble.{package}.cli",
                                *arguments,
                            ]
                        else:
                            directory = (
                                Path(REPO, "bin")
                                if entry == "repository"
                                else Path(self.ensemble, "bin")
                            )
                            command = [str(directory / f"ensemble-{root}"), *arguments]
                        self.help_only(command)
        self.assertEqual(self.snapshot(), before)

    def test_crew_help_without_credentials_services_terminal_or_manager(self):
        self.matrix("crew")

    def test_crew_help_with_malformed_test_owned_configuration(self):
        Path(self.ensemble, "config.local.toml").write_text("[malformed\n")
        self.matrix("crew")

    def test_session_help_without_credentials_services_terminal_or_manager(self):
        self.matrix("session")

    def test_session_help_with_malformed_test_owned_configuration(self):
        Path(self.ensemble, "config.local.toml").write_text("[malformed\n")
        self.matrix("session")

    def test_delegate_help_without_credentials_services_terminal_or_manager(self):
        self.matrix("delegate")

    def test_delegate_help_with_malformed_test_owned_configuration(self):
        Path(self.ensemble, "config.local.toml").write_text("[malformed\n")
        self.matrix("delegate")

    def test_orchestrator_help_without_credentials_services_terminal_or_manager(self):
        self.matrix("orchestrator")

    def test_orchestrator_help_with_malformed_test_owned_configuration(self):
        Path(self.ensemble, "config.local.toml").write_text("[malformed\n")
        self.matrix("orchestrator")
