""".githooks/pre-commit committing in throwaway git repos, with the pinned ruff or stand-ins."""

import functools
import os
import shutil
import subprocess
import unittest
from pathlib import Path

import ruff_hook

from tests.case import REPO, Case
from tests.utils.sandbox import git, git_run, python_path

HOOKS = os.path.join(REPO, ".githooks")
VERSION = ruff_hook.pinned_version(ruff_hook.WORKFLOW.read_text(encoding="utf-8"))
BAD = "import os\nx=1\ndef f( a ):\n  return  a\n"
GOOD = "x = 1\n\n\ndef f(a):\n    return a\n"


@functools.cache
def pinned_ruff():
    path = shutil.which("ruff")
    if path:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, check=False)
        if out.stdout.split()[-1:] == [VERSION]:
            return path
    if os.environ.get("GITHUB_ACTIONS"):
        raise AssertionError(f"CI must install ruff {VERSION}")
    raise unittest.SkipTest(f"ruff {VERSION} not on PATH")


class Hook(Case):
    def setUp(self):
        self.root = Path(self.home, "repo")
        self.root.mkdir()
        git("init", "-q", cwd=self.root)
        self.write("ruff.toml", '[lint]\nselect = ["F"]\n')
        self.path = os.environ.get("PATH", os.defpath)

    def write(self, name, text):
        (self.root / name).write_text(text)

    def read(self, name):
        return (self.root / name).read_text()

    def commit(self, *names, only=()):
        if names:
            git("add", "--", *names, cwd=self.root)
        return git_run(
            "-c",
            f"core.hooksPath={HOOKS}",
            "commit",
            "-qm",
            "c",
            *only,
            cwd=self.root,
            env={**os.environ, "PATH": self.path},
        )

    def committed(self, name):
        return git("show", f"HEAD:{name}", cwd=self.root) + "\n"

    def bin_dir(self, scripts, *, python=True):
        directory = Path(self.home, "bin")
        directory.mkdir()
        os.symlink(shutil.which("git"), directory / "git")
        for name, body in scripts.items():
            (directory / name).write_text(f"#!/bin/sh\n{body}\n")
            (directory / name).chmod(0o700)
        self.path = python_path(directory, str(directory)) if python else str(directory)

    def test_fixes_and_formats_the_staged_file(self):
        pinned_ruff()
        self.write("a.py", BAD)
        done = self.commit("a.py")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((self.committed("a.py"), self.read("a.py")), (GOOD, GOOD))
        self.assertIn("fixed a.py", done.stderr)

    def test_unfixable_lint_blocks_the_commit(self):
        pinned_ruff()
        self.write("a.py", "import os\nprint(undefined)\n")
        done = self.commit("a.py")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("F821", done.stderr)
        self.assertIn("commit aborted", done.stderr)
        self.assertNotEqual(git_run("rev-parse", "--verify", "HEAD", cwd=self.root).returncode, 0)

    def test_commit_with_paths_fixes_the_file_and_asks_to_commit_again(self):
        pinned_ruff()
        self.write("a.py", "x = 1\n")
        git("add", "a.py", cwd=self.root)
        git("commit", "-qm", "base", cwd=self.root)
        self.write("a.py", BAD)
        done = self.commit(only=("a.py",))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("run the same commit again", done.stderr)
        self.assertEqual(self.committed("a.py"), "x = 1\n")
        self.assertEqual(git("show", ":a.py", cwd=self.root) + "\n", "x = 1\n")
        self.assertEqual(self.read("a.py"), GOOD)
        done = self.commit(only=("a.py",))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.committed("a.py"), GOOD)
        self.assertEqual(git("status", "--porcelain", cwd=self.root), "?? ruff.toml")

    def test_unstaged_changes_and_other_files_stay_as_they_were(self):
        pinned_ruff()
        self.write("old.py", BAD)
        git("add", "old.py", cwd=self.root)
        git("commit", "-qm", "base", cwd=self.root)
        self.write("old.py", BAD + "y=2\n")
        self.write("untracked.py", BAD)
        self.write("notes.txt", "a  =  1\n")
        self.write("a.py", BAD)
        git("add", "a.py", "notes.txt", cwd=self.root)
        self.write("a.py", BAD + "z=3\n")
        done = self.commit()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.committed("a.py"), GOOD)
        self.assertEqual(self.read("a.py"), BAD + "z=3\n")
        self.assertIn("unstaged changes", done.stderr)
        self.assertEqual(self.committed("old.py"), BAD)
        self.assertEqual(self.read("old.py"), BAD + "y=2\n")
        self.assertEqual(self.read("untracked.py"), BAD)
        self.assertEqual(self.committed("notes.txt"), "a  =  1\n")

    def test_another_local_ruff_falls_back_to_uvx_at_the_pinned_version(self):
        real = pinned_ruff()
        log = Path(self.home, "uvx.log")
        self.bin_dir(
            {
                "ruff": 'echo "ruff 0.0.0"',
                "uvx": f'echo "$@" >> "{log}"\nshift\nexec "{real}" "$@"',
            }
        )
        self.write("a.py", BAD)
        done = self.commit("a.py")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.committed("a.py"), GOOD)
        self.assertEqual(log.read_text().splitlines()[0], f"ruff@{VERSION} --version")

    def test_without_ruff_or_uv_the_commit_goes_through_with_a_hint(self):
        self.bin_dir({})
        self.write("a.py", BAD)
        done = self.commit("a.py")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.committed("a.py"), BAD)
        self.assertIn(f"no ruff {VERSION} (neither ruff nor uv/uvx on PATH); skipped", done.stderr)

    def test_without_python_the_commit_goes_through_with_a_hint(self):
        self.bin_dir({}, python=False)
        self.write("a.py", BAD)
        done = self.commit("a.py")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.committed("a.py"), BAD)
        self.assertIn("python3 not found; skipped", done.stderr)
