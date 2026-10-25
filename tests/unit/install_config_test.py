"""Installer executable ownership and configuration migration, without launching tools."""

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
    def plan(self, current=None, previous=""):
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
            previous,
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

    def test_fresh_config_uses_selected_path(self):
        actual, changes, selected = self.plan()
        self.assertEqual(actual, selected)
        self.assertIn("[session.clients.opencode] executable = ", changes)

    def test_previous_installer_path_can_migrate(self):
        previous = "/old tools/opencode"
        actual, changes, selected = self.plan(previous, previous)
        self.assertEqual(actual, selected)
        self.assertIn("[session.clients.opencode] executable = ", changes)

    def test_legacy_private_default_can_migrate(self):
        actual, _, selected = self.plan(str(Path(self.home, ".ensemble/opencode/bin/opencode")))
        self.assertEqual(actual, selected)

    def test_user_path_is_preserved_and_not_claimed(self):
        actual, changes, _ = self.plan("/my/custom/opencode", "/previous/opencode")
        self.assertEqual(actual, "/my/custom/opencode")
        self.assertNotIn("[session.clients.opencode] executable = ", changes)

    def test_existing_matching_path_is_not_claimed(self):
        selected = str(Path(self.home, "selected tools/opencode"))
        actual, changes, _ = self.plan(selected)
        self.assertEqual(actual, selected)
        self.assertNotIn("[session.clients.opencode] executable = ", changes)
