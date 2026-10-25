""".github/ruff_hook.py: the pinned version and which ruff command runs it."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import ruff_hook

WORKFLOW = Path(__file__).resolve().parent / "workflows" / "test.yml"


class PinnedVersion(unittest.TestCase):
    def test_reads_the_pip_pins(self):
        text = (
            "style) pip install ruff==0.16.9 vulture==2.16 ;;\nrun: pip install ruff==0.16.9 x==1\n"
        )
        self.assertEqual(ruff_hook.pinned_version(text), "0.16.9")

    def test_missing_or_disagreeing_pins_are_none(self):
        self.assertIsNone(ruff_hook.pinned_version("run: pip install vulture==2.16\n"))
        self.assertIsNone(ruff_hook.pinned_version("ruff==0.16.9 ;;\nruff==0.17.0\n"))

    def test_the_real_workflow_pins_one_version(self):
        version = ruff_hook.pinned_version(WORKFLOW.read_text(encoding="utf-8"))
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")


class Runner(unittest.TestCase):
    def pick(self, installed, versions):
        def run(argv, **_):
            out = versions.get(argv[0], "")
            return SimpleNamespace(returncode=0 if out else 1, stdout=out)

        with (
            mock.patch.object(ruff_hook.shutil, "which", lambda name: installed.get(name)),
            mock.patch.object(ruff_hook.subprocess, "run", run),
        ):
            return ruff_hook.runner("0.16.9")

    def test_exact_local_ruff_wins(self):
        found = self.pick(
            {"ruff": "/bin/ruff", "uvx": "/bin/uvx"},
            {"/bin/ruff": "ruff 0.16.9\n", "/bin/uvx": "ruff 0.16.9\n"},
        )
        self.assertEqual(found, ["/bin/ruff"])

    def test_other_local_version_falls_back_to_uvx(self):
        found = self.pick(
            {"ruff": "/bin/ruff", "uvx": "/bin/uvx"},
            {"/bin/ruff": "ruff 0.15.0\n", "/bin/uvx": "ruff 0.16.9\n"},
        )
        self.assertEqual(found, ["/bin/uvx", "ruff@0.16.9"])

    def test_uv_without_uvx_runs_it_as_a_tool(self):
        found = self.pick({"uv": "/bin/uv"}, {"/bin/uv": "ruff 0.16.9\n"})
        self.assertEqual(found, ["/bin/uv", "tool", "run", "ruff@0.16.9"])

    def test_a_failing_uvx_falls_through(self):
        found = self.pick({"uvx": "/bin/uvx", "uv": "/bin/uv"}, {"/bin/uv": "ruff 0.16.9\n"})
        self.assertEqual(found, ["/bin/uv", "tool", "run", "ruff@0.16.9"])

    def test_rejected_commands_are_reported(self):
        tried = []
        with (
            mock.patch.object(ruff_hook.shutil, "which", {"ruff": "/bin/ruff"}.get),
            mock.patch.object(
                ruff_hook.subprocess,
                "run",
                lambda *_, **__: SimpleNamespace(returncode=0, stdout="ruff 0.15.0\n"),
            ),
        ):
            self.assertIsNone(ruff_hook.runner("0.16.9", tried))
        self.assertEqual(tried, ["/bin/ruff: ruff 0.15.0"])

    def test_nothing_usable_is_none(self):
        self.assertIsNone(self.pick({"ruff": "/bin/ruff"}, {"/bin/ruff": "ruff 0.15.0\n"}))
        self.assertIsNone(self.pick({}, {}))


if __name__ == "__main__":
    unittest.main()
