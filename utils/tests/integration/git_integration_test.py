"""Checkout discovery and common directories against isolated Git repositories."""

import os
import subprocess
import tempfile
import unittest
import unittest.mock

from ensemble.utils.git import common_dir

from utils.tests.case import Case
from utils.tests.sandbox import clean_env, git


class CommonDir(Case):
    def test_git_cannot_discover_a_repository_above_test_home_or_workspace(self):
        home, workspace = (os.path.join(self.clone, name) for name in ("home", "workspace"))
        for path in (home, workspace):
            os.mkdir(path)
        env = clean_env({**os.environ, "HOME": home, "ENSEMBLE_TEST_WORKSPACE": workspace})
        self.assertEqual(env["GIT_CEILING_DIRECTORIES"], self.clone)
        for directory in (home, workspace):
            with self.subTest(directory=directory):
                proc = subprocess.run(
                    ["git", "rev-parse", "--show-toplevel"],
                    cwd=directory,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(proc.returncode, 128, proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertIn("not a git repository", proc.stderr)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.clone, self.bare = (
            os.path.join(self.tmp, "clone"),
            os.path.join(self.tmp, "origin.git"),
        )
        git("init", "-q", "-b", "main", self.clone)
        git("-C", self.clone, "commit", "-q", "--allow-empty", "-m", "one")
        git("clone", "-q", "--bare", self.clone, self.bare)

    def add(self, branch="TASK-1-x", opts=()):
        wt = os.path.join(self.tmp, "w", branch)
        git("-C", self.clone, "worktree", "add", "-q", *opts, "-b", branch, wt)
        return wt

    def test_a_worktree_names_its_clones_git_dir(self):
        common = os.path.join(self.clone, ".git")
        self.assertEqual(common_dir(self.add()), common)
        self.assertEqual(common_dir(self.add("TASK-1-rel", ("--relative-paths",))), common)

    def test_a_crlf_git_file_is_read_as_git_reads_it(self):
        wt = self.add()
        gitdir = git("-C", wt, "rev-parse", "--path-format=absolute", "--git-dir")
        with open(os.path.join(wt, ".git"), "w", newline="") as f:
            f.write(f"gitdir: {gitdir}\r\n")
        self.assertEqual(common_dir(wt), os.path.join(self.clone, ".git"))

    def test_trailing_whitespace_is_part_of_the_path_as_for_git(self):
        wt = self.add()
        admin = git("-C", wt, "rev-parse", "--path-format=absolute", "--git-dir")
        fake, other = os.path.join(self.tmp, "fake-admin"), os.path.join(self.tmp, "other", ".git")
        os.makedirs(fake)
        os.makedirs(other)
        with open(os.path.join(fake, "commondir"), "w") as f:
            f.write(other + "\n")
        a = os.path.join(self.tmp, "a")
        os.makedirs(a)
        os.symlink(admin, os.path.join(a, "sym"))
        os.symlink(fake, os.path.join(a, "sym "))
        with open(os.path.join(wt, ".git"), "w") as f:
            f.write(f"gitdir: {a}/sym \n")
        self.assertEqual(common_dir(wt), other)

    def test_no_worktree_names_no_clone(self):
        self.add()
        plain, junk, orphan, bare = (
            os.path.join(self.tmp, d) for d in ("plain", "junk", "orphan", "bare")
        )
        for d in (plain, junk, orphan, bare):
            os.makedirs(d)
        for d, text in (
            (junk, "nonsense\n"),
            (orphan, f"gitdir: {self.tmp}/gone\n"),
            (bare, f"gitdir: {self.bare}\n"),
        ):
            with open(os.path.join(d, ".git"), "w") as f:
                f.write(text)
        for path in (
            self.clone,
            plain,
            junk,
            orphan,
            bare,
            os.path.join(self.tmp, "missing"),
        ):
            with self.subTest(path=path):
                self.assertIsNone(common_dir(path))

    def test_a_commondir_symlink_is_followed(self):
        wt = self.add()
        admin = git("-C", wt, "rev-parse", "--path-format=absolute", "--git-dir")
        path = os.path.join(admin, "commondir")
        target = path + "-target"
        os.rename(path, target)
        os.symlink(target, path)
        self.assertEqual(common_dir(wt), os.path.join(self.clone, ".git"))

    def test_a_file_replaced_by_a_fifo_at_open_is_rejected_without_waiting(self):
        wt = self.add()
        admin = git("-C", wt, "rev-parse", "--path-format=absolute", "--git-dir")
        open_file = os.open
        for path in (os.path.join(wt, ".git"), os.path.join(admin, "commondir")):
            with self.subTest(path=path):
                backup = path + "-backup"

                def replace(file, flags, *args, target=path, **kwargs):
                    if file == target:
                        self.assertTrue(flags & os.O_NONBLOCK)
                        os.rename(target, target + "-backup")
                        os.mkfifo(target)
                    return open_file(file, flags, *args, **kwargs)

                with unittest.mock.patch("os.open", side_effect=replace):
                    self.assertIsNone(common_dir(wt))
                os.unlink(path)
                os.rename(backup, path)
