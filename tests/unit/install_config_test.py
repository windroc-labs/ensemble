"""Installer fills missing executable settings and preserves existing ones."""

import io
import json
import os
import sys
import tomllib
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tests.case import Case
from tests.unit.install_script import program


class OpenCodeExecutableConfigTest(Case):
    def plan(self, current):
        config = Path(self.home, "config.local.toml")
        output = Path(self.home, "config.plan")
        selected = str(Path(self.home, "selected tools/opencode"))
        if current is not None:
            config.write_text(f"[session.clients.opencode]\nexecutable = {json.dumps(current)}\n")
        args = [
            str(config),
            str(output),
            "tmux",
            "opencode",
            selected,
            "lan",
            "http://localhost/v1",
            "m",
            "65536",
            "",
            "",
            "",
            "",
            "",
            "",
        ]
        stdout = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["-c", *args]),
            mock.patch.dict(os.environ, {"HOME": self.home}),
            redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            exec(program("config_plan"), {})
        self.assertEqual(raised.exception.code, 3)
        data = tomllib.loads(output.read_text())["session"]["clients"]["opencode"]
        return data["executable"], stdout.getvalue(), selected

    def test_only_missing_executable_is_filled(self):
        for current in (
            None,
            "",
            "/my/custom/opencode",
            str(Path(self.home, ".ensemble/opencode/bin/opencode")),
        ):
            with self.subTest(current=current):
                actual, changes, selected = self.plan(current)
                self.assertEqual(actual, current or selected)
                self.assertEqual("[session.clients.opencode] executable = " in changes, not current)


class GitHubCredentialConfigTest(Case):
    def test_engineer_declaration_fills_permissions_and_keeps_custom_values(self):
        table = "orchestrator.roles.engineer.credentials.github"
        expected = {
            "contents": "write",
            "pull_requests": "write",
            "workflows": "write",
            "checks": "read",
            "actions": "read",
            "statuses": "read",
        }
        for custom in (False, True):
            with self.subTest(custom=custom):
                config = Path(self.home, "config.local.toml")
                output = Path(self.home, "config.plan")
                config.write_text(
                    f'[{table}.permissions]\nstatuses = "write"\nissues = "write"\n'
                    if custom
                    else ""
                )
                args = [
                    str(config),
                    str(output),
                    "docker",
                    "claude",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    f"{table}\tapp\tensemble-test-bot",
                    "",
                ]
                with (
                    mock.patch.object(sys, "argv", ["-c", *args]),
                    redirect_stdout(io.StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    exec(program("config_plan"), {})
                self.assertEqual(raised.exception.code, 3)
                data = tomllib.loads(output.read_text())
                github = data["orchestrator"]["roles"]["engineer"]["credentials"]["github"]
                self.assertEqual(github["app"], "ensemble-test-bot")
                self.assertEqual(
                    github["permissions"],
                    expected | ({"statuses": "write", "issues": "write"} if custom else {}),
                )
                self.assertNotIn("session", data)
