"""Installer fills missing executable settings and preserves existing ones."""

import io
import json
import os
import sys
import tomllib
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tests.case import REPO, Case


class OpenCodeExecutableConfigTest(Case):
    def plan(self, current):
        config = Path(self.home, "config.local.toml")
        output = Path(self.home, "config.plan")
        selected = str(Path(self.home, "selected tools/opencode"))
        if current is not None:
            config.write_text(f"[session.clients.opencode]\nexecutable = {json.dumps(current)}\n")
        text = Path(REPO, "scripts/install.sh").read_text()
        body = text.split("config_plan() {\n", 1)[1].split("\n}\n", 1)[0]
        program = body.split("python3 -I -c '\n", 1)[1].split("' ", 1)[0]
        args = [
            str(config),
            str(output),
            "tmux",
            "",
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
            exec(program, {})
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
