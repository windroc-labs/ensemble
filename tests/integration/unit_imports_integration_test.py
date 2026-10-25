"""Ruff TID251 rule samples and the interpreter gate import boundary."""

import json
import subprocess
import unittest
from pathlib import Path

from tests.case import REPO, Case
from tests.unit_imports import CONFIG, _unit_files

GATE_CONFIG = 'lint.flake8-tidy-imports.banned-api = {isolation = {msg = "Tests cannot access interpreter gates"}}'


class UnitImports(Case):
    def test_test_files_cannot_import_the_interpreter_gate(self):
        root = Path(REPO)
        files = sorted(
            str(path)
            for path in root.rglob("*.py")
            if "tests" in path.relative_to(root).parts
            or path.parent == root / ".github"
            and path.name.endswith("_test.py")
        )
        sample = Path(self.home, "gate.py")
        sample.write_text("import isolation\nfrom isolation import Gate\n")
        for inputs, expected in ((files, []), ([str(sample)], ["TID251", "TID251"])):
            proc = subprocess.run(
                [
                    "ruff",
                    "check",
                    "--isolated",
                    "--no-cache",
                    "--select",
                    "TID251",
                    "--config",
                    GATE_CONFIG,
                    "--output-format",
                    "json",
                    *inputs,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertIn(proc.returncode, (0, 1), proc.stderr)
            self.assertEqual([item["code"] for item in json.loads(proc.stdout)], expected)

    def test_github_unit_modules_are_selected_without_selecting_the_runner(self):
        root = Path(self.home)
        github = root / ".github"
        github.mkdir()
        sample = github / "escape_test.py"
        sample.write_text("import subprocess\n")
        (github / "run_tests.py").write_text("import subprocess\n")
        files = _unit_files(root)
        self.assertEqual(files, [str(sample)])
        proc = subprocess.run(
            [
                "ruff",
                "check",
                "--isolated",
                "--select",
                "TID251",
                "--config",
                CONFIG,
                "--output-format",
                "json",
                *files,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual([item["code"] for item in json.loads(proc.stdout)], ["TID251"])

    def test_the_rule_rejects_process_socket_multiprocessing_and_pool_imports_and_calls(self):
        for name, text, count in (
            ("sample.py", "import subprocess\nfrom socket import socket\n", 2),
            (
                "pools.py",
                "import multiprocessing\nfrom concurrent.futures import ProcessPoolExecutor\n"
                "import concurrent.futures\nconcurrent.futures.ProcessPoolExecutor()\n",
                3,
            ),
        ):
            with self.subTest(name=name):
                sample = Path(self.home, name)
                sample.write_text(text)
                proc = subprocess.run(
                    [
                        "ruff",
                        "check",
                        "--isolated",
                        "--select",
                        "TID251",
                        "--config",
                        CONFIG,
                        "--output-format",
                        "json",
                        str(sample),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertEqual(
                    [item["code"] for item in json.loads(proc.stdout)], ["TID251"] * count
                )


if __name__ == "__main__":
    unittest.main()
