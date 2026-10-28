"""Ruff TID251 rule samples and the interpreter gate import boundary."""

import json
import subprocess
import unittest
from pathlib import Path

from tests.case import REPO, Case
from tests.unit_imports import CONFIG

GATE_CONFIG = 'lint.flake8-tidy-imports.banned-api = {isolation = {msg = "Tests cannot access interpreter gates"}}'


class UnitImports(Case):
    def codes(self, config, *files):
        proc = subprocess.run(
            [
                "ruff",
                "check",
                "--isolated",
                "--no-cache",
                "--select",
                "TID251",
                "--config",
                config,
                "--output-format",
                "json",
                *files,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        found = [item["code"] for item in json.loads(proc.stdout)]
        self.assertEqual(proc.returncode, 1 if found else 0, proc.stderr)
        return found

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
            self.assertEqual(self.codes(GATE_CONFIG, *inputs), expected)

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
                self.assertEqual(self.codes(CONFIG, str(sample)), ["TID251"] * count)


if __name__ == "__main__":
    unittest.main()
