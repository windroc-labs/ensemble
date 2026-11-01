"""Harness and Composer caller environments in source and installed package layouts. The harness
also imports its session class (here the test's, under REPO).
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from tests.delegate.case import REPO, SRC, Case
from tests.utils.sandbox import clean_env


class ChildEnvironments(Case):
    def caller_environments(self, *, installed=False):
        roots = tuple(SRC[name] for name in ("delegate", "session", "utils"))
        package_root = Path(self.home, "site-packages")
        if installed:
            for name, source in zip(("delegate", "session", "utils"), roots, strict=True):
                shutil.copytree(
                    Path(source, "ensemble", name),
                    package_root / "ensemble" / name,
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
            roots = (str(package_root),)
        result = subprocess.run(
            [
                sys.executable,
                "-P",
                "-S",
                os.path.join(REPO, "tests", "delegate", "fakes", "caller_environment.py"),
                self.home,
                str(package_root) if installed else "",
            ],
            cwd=self.home,
            env=clean_env({**os.environ, "HOME": self.home}, *roots, REPO),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assert_environments(self, actual, expected, *, harness):
        self.assertEqual(actual["harness"], harness)
        for label, commands in (
            ("commands", ["command"]),
            ("no commands", []),
        ):
            with self.subTest(caller=label):
                self.assertEqual(
                    actual["composer"][label],
                    {"env": expected if commands else {}, "commands": commands},
                )

    def test_source_callers_supply_the_shared_import_root_once(self):
        owned = (SRC["delegate"],)
        self.assert_environments(
            self.caller_environments(),
            {"PYTHONPATH": os.pathsep.join(owned)},
            harness={"PYTHONPATH": os.pathsep.join((*owned, REPO))},
        )

    def test_installed_callers_supply_no_pythonpath(self):
        self.assert_environments(
            self.caller_environments(installed=True), {}, harness={"PYTHONPATH": REPO}
        )
