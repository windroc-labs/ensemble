"""src/broker/internal/launcher/assets/githook-forward and the git wrapper, run by real git."""

import os
import shutil
import subprocess
from pathlib import Path

from tests.case import REPO, Case

ASSETS = Path(REPO, "src/broker/internal/launcher/assets")


class GithookForward(Case):
    def setUp(self):
        self.env = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "a",
            "GIT_AUTHOR_EMAIL": "a@example.com",
            "GIT_COMMITTER_NAME": "a",
            "GIT_COMMITTER_EMAIL": "a@example.com",
        }
        self.hooks = Path(self.home, "hooks")
        self.hooks.mkdir()
        for name in ("pre-commit", "commit-msg"):
            shutil.copy(ASSETS / "githook-forward", self.hooks / name)
        self.work = Path(self.home, "work")
        self.git("init", "-q", str(self.work), cwd=self.home)
        (self.work / ".git/info/exclude").write_text(".githooks/\n.husky/\n")

    def git(self, *args, cwd=None):
        # Command scope, as the image's git wrapper sets it.
        return subprocess.run(
            ["git", "-c", f"core.hooksPath={self.hooks}", *args],
            cwd=cwd or self.work,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def repo_hook(self, directory, name, program):
        (self.work / directory).mkdir(exist_ok=True)
        shutil.copy(program, self.work / directory / name)

    def commit(self, message="c"):
        return self.git("commit", "-q", "--allow-empty", "-m", message)

    def test_without_a_repository_hook_git_goes_on(self):
        self.assertEqual(self.commit().returncode, 0)

    def test_the_repository_hook_of_the_same_name_decides(self):
        self.repo_hook(".githooks", "commit-msg", "/bin/false")
        self.assertNotEqual(self.commit().returncode, 0)
        self.repo_hook(".githooks", "commit-msg", "/bin/true")
        self.repo_hook(".githooks", "pre-merge-commit", "/bin/false")
        self.assertEqual(self.commit().returncode, 0)

    def test_the_repository_hook_gets_git_s_arguments(self):
        # cat prints the file git names as commit-msg's argument: the message.
        self.repo_hook(".githooks", "commit-msg", "/bin/cat")
        proc = self.commit("hello from the message")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("hello from the message", proc.stdout + proc.stderr)

    def test_a_non_executable_repository_hook_is_skipped(self):
        self.repo_hook(".githooks", "pre-commit", "/bin/false")
        os.chmod(self.work / ".githooks/pre-commit", 0o644)
        self.assertEqual(self.commit().returncode, 0)

    def test_a_repository_hooks_path_replaces_githooks(self):
        self.git("config", "core.hooksPath", ".husky")
        self.repo_hook(".husky", "pre-commit", "/bin/false")
        self.repo_hook(".githooks", "pre-commit", "/bin/true")
        self.assertNotEqual(self.commit().returncode, 0)

    def test_a_config_file_naming_the_image_directory_falls_back_to_githooks(self):
        self.git("config", "core.hooksPath", str(self.hooks))
        self.repo_hook(".githooks", "pre-commit", "/bin/false")
        self.assertNotEqual(self.commit().returncode, 0)
        self.repo_hook(".githooks", "pre-commit", "/bin/true")
        self.assertEqual(self.commit().returncode, 0)


class GitWrapper(Case):
    def setUp(self):
        self.bin = Path(self.home, "bin")
        self.bin.mkdir()
        shutil.copy(ASSETS / "git", self.bin / "git")
        self.work = Path(self.home, "work")
        self.work.mkdir()

    def wrapped(self, *args, path=None):
        env = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "PATH": path or f"{self.bin}:{os.environ['PATH']}",
        }
        return subprocess.run(
            [str(self.bin / "git"), *args],
            cwd=self.work,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_it_sets_the_image_hooks_path_above_config_files(self):
        proc = self.wrapped("config", "--show-scope", "--get-all", "core.hooksPath")
        self.assertEqual(proc.stdout, "command\t/opt/ensemble/githooks\n")

    def test_a_later_command_line_value_still_wins(self):
        proc = self.wrapped("-c", "core.hooksPath=/dev/null", "config", "core.hooksPath")
        self.assertEqual(proc.stdout, "/dev/null\n")

    def test_without_another_git_on_path_it_fails(self):
        proc = self.wrapped("--version", path=str(self.bin))
        self.assertEqual(proc.returncode, 127)
        self.assertIn("no git on PATH", proc.stderr)
