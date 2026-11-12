"""src/broker/internal/launcher/assets/githook-forward run by real git through core.hooksPath."""

import os
import shutil
import subprocess
from pathlib import Path

from tests.case import REPO, Case

FORWARD = Path(REPO, "src/broker/internal/launcher/assets/githook-forward")


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
        hooks = Path(self.home, "hooks")
        hooks.mkdir()
        for name in ("pre-commit", "commit-msg"):
            shutil.copy(FORWARD, hooks / name)
        self.work = Path(self.home, "work")
        self.git("init", "-q", str(self.work), cwd=self.home)
        self.git("config", "core.hooksPath", str(hooks))
        self.repo_hooks = self.work / ".githooks"
        self.repo_hooks.mkdir()
        (self.work / ".git/info/exclude").write_text(".githooks/\n")

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", *args], cwd=cwd or self.work, env=self.env, capture_output=True, text=True
        )

    def commit(self):
        return self.git("commit", "-q", "--allow-empty", "-m", "c").returncode

    def test_without_a_repository_hook_git_goes_on(self):
        self.assertEqual(self.commit(), 0)

    def test_the_repository_hook_of_the_same_name_decides(self):
        shutil.copy("/bin/false", self.repo_hooks / "commit-msg")
        self.assertNotEqual(self.commit(), 0)
        shutil.copy("/bin/true", self.repo_hooks / "commit-msg")
        shutil.copy("/bin/false", self.repo_hooks / "post-commit")
        self.assertEqual(self.commit(), 0)

    def test_a_non_executable_repository_hook_is_skipped(self):
        shutil.copy("/bin/false", self.repo_hooks / "pre-commit")
        os.chmod(self.repo_hooks / "pre-commit", 0o644)
        self.assertEqual(self.commit(), 0)
