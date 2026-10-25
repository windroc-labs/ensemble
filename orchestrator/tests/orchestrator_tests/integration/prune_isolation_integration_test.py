"""Real Git and interpreter contracts in the test sandbox."""

import os
import shutil
import subprocess
from unittest import mock

from orchestrator_tests.prune_fixtures import PruneScenario as UnitPruneScenario
from orchestrator_tests.prune_fixtures import gql_for, write

from delegate.tests.workdir import checkout_path
from utils.tests.sandbox import clean_env, git, git_run


class Git:
    def __init__(self, *, real=False):
        self.real, self.calls = real, []

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        if not self.real:
            raise AssertionError(f"git ran: {argv}")
        return git_run(*argv[1:], timeout=timeout)


class PruneScenario(UnitPruneScenario):
    def real_clone(self, at=None):
        seed, bare, clone = (
            os.path.join(self.root, "seed"),
            os.path.join(self.root, "origin.git"),
            at or os.path.join(self.root, "clone"),
        )
        if not os.path.isdir(bare):
            git("init", "-q", "-b", "main", seed)
            git(
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@t",
                "-C",
                seed,
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "one",
            )
            git("clone", "-q", "--bare", seed, bare)
        git("clone", "-q", bare, clone)
        return clone

    def git_env(self):
        return mock.patch.dict(
            os.environ,
            {
                **clean_env(),
            },
            clear=True,
        )


class RepositoryCleanup(PruneScenario):
    def test_finished_worktrees_go_with_their_records_and_pushed_branches(self):
        with self.git_env():
            clone = self.real_clone()
            workdir = self.cfg.issue_workdir("TASK-49")
            x, y = (
                checkout_path(workdir, "o", "n", "TASK-49"),
                checkout_path(workdir, "p", "m", "TASK-49"),
            )
            git("-C", clone, "worktree", "add", "-q", "-b", "TASK-49-x", x, "origin/main")
            git(
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@t",
                "-C",
                x,
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "x",
            )
            git("-C", x, "push", "-q", "origin", "TASK-49-x")
            git("-C", clone, "worktree", "add", "-q", "-b", "TASK-49-y", y, "origin/main")
            git(
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@t",
                "-C",
                y,
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "y",
            )
            git("-C", clone, "branch", "TASK-49-z", "origin/main")
            git("-C", clone, "branch", "TASK-490-q", "origin/main")
            own = os.path.join(self.root, "own")
            git("-C", clone, "worktree", "add", "-q", "-b", "mine", own)
            git("-C", clone, "worktree", "lock", own)
            shutil.move(own, own + "-unmounted")
            write(os.path.join(clone, "dirty.txt"), "x")
            before = [
                git("-C", clone, *a)
                for a in (
                    ["rev-parse", "HEAD"],
                    ["branch", "--show-current"],
                    ["status", "--porcelain"],
                )
            ]
            run = Git(real=True)
            code, out = self.prune(
                gql_for({"TASK-49": ("Done", [(30, "Done")])}), git=run, writable=(self.work,)
            )
            self.assertEqual(code, 0, out)
            self.assertEqual(
                out.splitlines(),
                [
                    "prune-removed TASK-49 what=agent dir",
                    f"prune-removed TASK-49 what=branch TASK-49-x in {clone}",
                    f"prune-skip TASK-49 reason=branch TASK-49-y in {clone} kept: not pushed",
                    f"prune-removed TASK-49 what=branch TASK-49-z in {clone}",
                ],
            )
            self.assertIsNone(self.checkouts("TASK-49"))
            listing = git("-C", clone, "worktree", "list", "--porcelain")
            self.assertNotIn(x, listing)
            self.assertNotIn(y, listing)
            self.assertIn(f"worktree {own}", listing)
            self.assertEqual(
                sorted(git("-C", clone, "branch", "--format=%(refname:short)").split()),
                ["TASK-49-y", "TASK-490-q", "main", "mine"],
            )
            self.assertEqual(
                [
                    git("-C", clone, *a)
                    for a in (
                        ["rev-parse", "HEAD"],
                        ["branch", "--show-current"],
                        ["status", "--porcelain"],
                    )
                ],
                before,
            )
            self.assertTrue(run.calls)
            self.assertTrue(
                all(c[5:8] == ["-C", clone, f"--git-dir={clone}/.git"] for c in run.calls),
                run.calls,
            )


class RetentionCleanup(PruneScenario):
    def test_a_clone_an_agent_run_could_write_gets_no_git_run(self):
        with self.git_env():
            for name, at, writable in (
                ("in work/", os.path.join(self.work, "TASK-49", "evil"), (self.work,)),
                ("in a temp dir, by default", os.path.join(self.root, "tmp-clone"), None),
            ):
                with self.subTest(name):
                    clone = self.real_clone(at)
                    wt = checkout_path(self.cfg.issue_workdir("TASK-49"), "o", "n", "TASK-49")
                    git("-C", clone, "worktree", "add", "-q", "-b", "TASK-49-x", wt, "origin/main")
                    run = Git(real=True)
                    code, out = self.prune(
                        gql_for({"TASK-49": ("Done", [(30, "Done")])}), git=run, writable=writable
                    )
                    self.assertEqual((code, run.calls), (0, []))
                    self.assertEqual(out.splitlines()[0], "prune-removed TASK-49 what=agent dir")
                    self.assertTrue(
                        out.splitlines()[1].startswith(
                            f"prune-skip TASK-49 reason={clone} is under "
                        ),
                        out,
                    )
                    self.assertFalse(os.path.lexists(wt))

    def test_a_git_failure_in_the_clone_is_an_error(self):
        with self.git_env():
            clone = self.real_clone()
            git(
                "-C",
                clone,
                "worktree",
                "add",
                "-q",
                "-b",
                "TASK-49-x",
                checkout_path(self.cfg.issue_workdir("TASK-49"), "o", "n", "TASK-49"),
            )

        def failing(argv, _timeout):
            return subprocess.CompletedProcess(argv, 1, "", "fatal: boom")

        code, out = self.prune(
            gql_for({"TASK-49": ("Done", [(30, "Done")])}), git=failing, writable=(self.work,)
        )
        self.assertEqual(code, 3)
        self.assertEqual(out.splitlines()[0], "prune-removed TASK-49 what=agent dir")
        self.assertTrue(
            out.splitlines()[1].startswith("prune-error TASK-49 error=GitError: git --git-dir="),
            out,
        )
        self.assertTrue(out.splitlines()[1].endswith("worktree prune: fatal: boom"), out)

    def test_dry_run_plans_the_clone_step_and_runs_no_git(self):
        with self.git_env():
            clone = self.real_clone()
            wt = checkout_path(self.cfg.issue_workdir("TASK-49"), "o", "n", "TASK-49")
            git("-C", clone, "worktree", "add", "-q", "-b", "TASK-49-x", wt)
        code, out = self.prune(
            gql_for({"TASK-49": ("Done", [(30, "Done")])}), dry=True, writable=(self.work,)
        )
        self.assertEqual(
            (code, out.splitlines()),
            (
                0,
                [
                    "prune-plan TASK-49 what=delete the agent dir",
                    (
                        f"prune-plan TASK-49 what=prune {clone}'s worktree records and pushed TASK-49-* branches"
                    ),
                ],
            ),
        )
        self.assertTrue(os.path.isfile(os.path.join(wt, ".git")))
