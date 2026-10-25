"""Repository tool behavior against temporary Git repositories."""

import io
import json
import os
import shutil
import tempfile
import threading
import unittest
import unittest.mock

from delegate.tests.case import Case
from delegate.tests.repo import FETCH, LOGIN, URL, Fake, fail, info, ok, pr_list, repo
from utils.tests.sandbox import git, git_run


class Real(Fake):
    """Real git; `gh` as in Fake; an origin's URL reads as URL."""

    def __call__(self, argv, timeout):
        if argv[0] != "git":
            return super().__call__(argv, timeout)
        self.calls.append(argv)
        self.timeouts.append(timeout)
        for prefix, res in self.answers:
            if callable(prefix) and prefix(argv):
                return res
        res = git_run(*argv[1:], timeout=timeout)
        if argv[-3:] == ["remote", "get-url", "origin"] and res.returncode == 0:
            return ok(URL + "\n")
        return res


def upstream(wt):
    res = git_run("-C", wt, "rev-parse", "--abbrev-ref", "@{upstream}")
    return res.stdout.strip() if res.returncode == 0 else None


class Clone(Case):
    """A real clone of a bare repo, its origin reading as github.com/o/n."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.seed, self.bare, self.clone = (
            os.path.join(self.tmp, d) for d in ("seed", "origin.git", "clone")
        )
        git("init", "-q", "-b", "main", self.seed)
        git("-C", self.seed, "commit", "-q", "--allow-empty", "-m", "one")
        git("-C", self.seed, "branch", "TASK-1-remote")
        git("clone", "-q", "--bare", self.seed, self.bare)
        git("-C", self.seed, "remote", "add", "origin", self.bare)
        git("clone", "-q", self.bare, self.clone)
        self.dir = os.path.join(self.tmp, "work", "src")
        self.wt = os.path.join(self.dir, "o", "n-TASK-1-x")
        self.none = os.devnull

    def listing(self, *paths):
        """A config.toml whose trusted_dirs lists `paths`."""
        path = os.path.join(self.tmp, "config.toml")
        with open(path, "w") as f:
            f.write(f"trusted_dirs = {json.dumps(paths)}\n")
        return path

    def advance(self):
        git("-C", self.seed, "commit", "-q", "--allow-empty", "-m", "two")
        git("-C", self.seed, "push", "-q", "origin", "main")
        return git("-C", self.seed, "rev-parse", "HEAD")


class Local(Clone):
    def worktree(self, branch="TASK-1-x", spec=None, base=None, config=None, base_branch=None):
        run, out, err = Real([info()]), io.StringIO(), io.StringIO()
        code = repo.main(
            [
                "worktree",
                "--dir",
                base or self.dir,
                "--branch",
                branch,
                *([] if base_branch is None else ["--base", base_branch]),
                spec or self.clone,
            ],
            config=config or self.none,
            local=self.none,
            temp=(),
            run=run,
            out=out,
            err=err,
        )
        return code, json.loads(out.getvalue()) if code == 0 else err.getvalue(), run

    def test_new_local_branch_uses_base_without_main_only_commits(self):
        git("-C", self.seed, "checkout", "-q", "-b", "re-architecture")
        git("-C", self.seed, "commit", "-q", "--allow-empty", "-m", "base only")
        base = git("-C", self.seed, "rev-parse", "HEAD")
        git("-C", self.seed, "push", "-q", "origin", "re-architecture")
        git("-C", self.seed, "checkout", "-q", "main")
        main_only = self.advance()
        for branch, selected, want in (
            ("TASK-1-x", "re-architecture", base),
            ("TASK-2-x", None, main_only),
        ):
            with self.subTest(base=selected):
                code, result, _ = self.worktree(branch, base_branch=selected)
                self.assertEqual(code, 0, result)
                self.assertEqual(result["commit"], want)
                self.assertIsNone(upstream(result["worktree"]))
                if selected:
                    self.assertNotIn(main_only, git("-C", result["worktree"], "rev-list", "HEAD"))

    def test_explicit_base_preserves_local_and_remote_worktree_branches(self):
        local = self.advance()
        git("-C", self.clone, "fetch", "-q", "origin")
        git("-C", self.clone, "branch", "--no-track", "TASK-1-remote", "origin/main")
        for branch, want, tracking in (
            ("TASK-1-remote", local, None),
            (
                "TASK-2-remote",
                git("-C", self.seed, "rev-parse", "TASK-1-remote"),
                "origin/TASK-2-remote",
            ),
        ):
            with self.subTest(branch=branch):
                if tracking:
                    git("-C", self.seed, "push", "-q", "origin", "TASK-1-remote:TASK-2-remote")
                code, result, _ = self.worktree(branch, base_branch="missing")
                self.assertEqual(code, 0, result)
                self.assertEqual(result["commit"], want)
                self.assertEqual(upstream(result["worktree"]), tracking)
                code, reused, run = self.worktree(branch, base_branch="main")
                self.assertEqual(code, 0, reused)
                self.assertEqual(reused["commit"], want)
                self.assertFalse(any("add" in c or "checkout" in c for c in run.calls))

    def test_missing_remote_base_leaves_no_local_branch(self):
        code, err, _ = self.worktree(base_branch="missing")
        self.assertEqual(code, 1, err)
        self.assertIn("origin/missing", err)
        self.assertEqual(git("-C", self.clone, "branch", "--list", "TASK-1-x"), "")
        self.assertFalse(os.path.exists(self.wt))

    def test_a_path_or_a_tilde_path(self):
        head = git("-C", self.clone, "rev-parse", "HEAD")
        with unittest.mock.patch.dict(os.environ, {"HOME": self.tmp}):
            for i, spec in enumerate((self.clone, "~/clone")):
                with self.subTest(spec=spec):
                    base = os.path.join(self.tmp, f"w{i}")
                    wt = os.path.join(base, "o", f"n-TASK-1-{i}")
                    code, r, _ = self.worktree(f"TASK-1-{i}", spec=spec, base=base)
                    self.assertEqual(code, 0, r)
                    self.assertEqual(
                        r,
                        {
                            "repo": "o/n",
                            "host": "github.com",
                            "default": "main",
                            "branch": f"TASK-1-{i}",
                            "commit": head,
                            "worktree": wt,
                            "permalink_base": f"https://github.com/o/n/blob/{head}/",
                            "push": True,
                        },
                    )
                    self.assertTrue(os.path.isfile(os.path.join(wt, ".git")))
                    self.assertEqual(
                        git("-C", wt, "rev-parse", "--path-format=absolute", "--git-common-dir"),
                        os.path.join(self.clone, ".git"),
                    )

    def test_not_a_clone_is_invalid(self):
        linked, plain, bare = (os.path.join(self.tmp, d) for d in ("linked", "plain", "no-origin"))
        git("-C", self.clone, "worktree", "add", "-q", "-b", "TASK-9-y", linked)
        os.makedirs(plain)
        git("init", "-q", bare)
        for spec in (os.path.join(self.tmp, "missing"), plain, linked, bare):
            with self.subTest(spec=spec):
                code, err, run = self.worktree(spec=spec)
                self.assertEqual(code, 2, err)
                self.assertFalse(run.ran("gh"))
                self.assertFalse(os.path.exists(self.dir))

    def test_fetch_leaves_the_clone_alone(self):
        with open(os.path.join(self.clone, "dirty.txt"), "w") as f:
            f.write("x")
        git("-C", self.clone, "commit", "-q", "--allow-empty", "-m", "local")

        def state():
            return [
                git("-C", self.clone, *a)
                for a in (
                    ["rev-parse", "main"],
                    ["branch", "--show-current"],
                    ["status", "--porcelain"],
                )
            ]

        before = state()
        new = self.advance()
        code, r, _ = self.worktree()
        self.assertEqual((code, r["commit"]), (0, new))
        self.assertEqual(git("-C", self.clone, "rev-parse", "origin/main"), new)
        self.assertEqual(state(), before)

    def test_branch_rules_via_worktree_add(self):
        old = git("-C", self.clone, "rev-parse", "HEAD")
        git("-C", self.clone, "branch", "TASK-1-local")
        new = self.advance()
        for branch, commit, tracks in (
            ("TASK-1-local", old, None),
            ("TASK-1-remote", old, "origin/TASK-1-remote"),
            ("TASK-1-new", new, None),
        ):
            with self.subTest(branch=branch):
                base = os.path.join(self.tmp, branch)
                wt = os.path.join(base, "o", f"n-{branch}")
                code, r, run = self.worktree(branch, base=base)
                self.assertEqual((code, r["commit"], r["worktree"]), (0, commit, wt))
                self.assertEqual(run.timeout("git", "-C", self.clone, "worktree", "add"), repo.LONG)
                self.assertEqual(
                    (git("-C", wt, "branch", "--show-current"), upstream(wt)), (branch, tracks)
                )

    def test_reuse_fetches_only(self):
        self.assertEqual(self.worktree()[0], 0)
        new = self.advance()
        code, r, run = self.worktree()
        self.assertEqual((code, r["worktree"]), (0, self.wt))
        self.assertTrue(run.ran("git", "-C", self.wt, *FETCH))
        self.assertFalse(any("add" in c or "checkout" in c for c in run.calls))
        self.assertEqual(git("-C", self.clone, "rev-parse", "origin/main"), new)

    def test_a_worktree_of_another_clone_is_invalid(self):
        other = os.path.join(self.tmp, "clone2")
        git("clone", "-q", self.bare, other)
        git("-C", other, "worktree", "add", "-q", "-b", "TASK-1-x", self.wt)
        code, err, _ = self.worktree()
        self.assertEqual(code, 2)
        self.assertIn("not a checkout of github.com/o/n", err)

    def test_a_full_clone_at_the_place_is_used(self):
        git("clone", "-q", self.bare, self.wt)
        code, r, run = self.worktree()
        self.assertEqual((code, r["worktree"]), (0, self.wt))
        self.assertTrue(os.path.isdir(os.path.join(self.wt, ".git")))
        self.assertTrue(
            run.ran("git", "-C", self.wt, "checkout", "--no-track", "-b", "TASK-1-x", "origin/main")
        )
        self.assertFalse(run.ran("git", "-C", self.clone, "worktree", "add"))

    def test_a_branch_checked_out_elsewhere_is_invalid(self):
        other = os.path.join(self.tmp, "elsewhere")
        git("-C", self.clone, "worktree", "add", "-q", "-b", "TASK-1-x", other)
        code, err, _ = self.worktree()
        self.assertEqual(code, 2)
        self.assertIn(f"TASK-1-x is checked out in {other}", err)
        self.assertFalse(os.path.exists(self.wt))

    def test_two_runs_at_once_on_one_clone(self):
        self.advance()
        results = {}

        def go(branch):
            results[branch] = repo.worktree(
                self.clone,
                branch,
                os.path.join(self.tmp, branch),
                run=Real([info()]),
                temp=(),
            )

        branches = ("TASK-1-a", "TASK-2-b")
        threads = [threading.Thread(target=go, args=(b,)) for b in branches]
        previous_hook = threading.excepthook

        def capture(args):
            if args.thread in threads:
                results[branches[threads.index(args.thread)]] = args.exc_value
            else:
                previous_hook(args)

        with unittest.mock.patch("threading.excepthook", new=capture):
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        for b, r in results.items():
            self.assertIsInstance(r, dict, r)
            self.assertEqual(git("-C", r["worktree"], "branch", "--show-current"), b)
        self.assertEqual(len(results), 2)

    def test_name_names_the_worktree_for_worktree_and_status(self):
        wt = os.path.join(self.dir, "o", "n-TASK-7")
        run, out, err = Real([info()]), io.StringIO(), io.StringIO()
        code = repo.main(
            ["worktree", "--dir", self.dir, "--branch", "TASK-7-x", "--name", "TASK-7", self.clone],
            config=self.none,
            local=self.none,
            temp=(),
            run=run,
            out=out,
            err=err,
        )
        self.assertEqual((code, json.loads(out.getvalue())["worktree"]), (0, wt), err.getvalue())
        admin = git("-C", wt, "rev-parse", "--path-format=absolute", "--git-dir")
        self.assertEqual(admin, os.path.join(self.clone, ".git", "worktrees", "n-TASK-7"))
        for argv, want in ((["--name", "TASK-7"], 0), ([], 2)):
            with self.subTest(argv=argv):
                run, out, err = Real([LOGIN, pr_list([]), info()]), io.StringIO(), io.StringIO()
                code = repo.main(
                    ["status", "--dir", self.dir, "--branch", "TASK-7-x", *argv, self.clone],
                    config=self.none,
                    local=self.none,
                    run=run,
                    out=out,
                    err=err,
                )
                self.assertEqual(code, want, err.getvalue())
                if want == 0:  # status with a local path
                    self.assertEqual(
                        {k: json.loads(out.getvalue())[k] for k in ("pr", "base")},
                        {"pr": None, "base": "main"},
                    )
                    self.assertTrue(run.ran("gh", "pr", "list", "--repo", "github.com/o/n"))


class LocalSafety(Clone):
    def worktree(self, fail_on=None, temp=(), config=None, spec=None, local=None):
        run, out, err = (
            Real([info(), *([(fail_on, fail("boom"))] if fail_on else [])]),
            io.StringIO(),
            io.StringIO(),
        )
        code = repo.main(
            ["worktree", "--dir", self.dir, "--branch", "TASK-1-x", spec or self.clone],
            config=config or self.none,
            local=local or self.none,
            temp=temp,
            run=run,
            out=out,
            err=err,
        )
        return code, err.getvalue()

    def file_commit(self):
        with open(os.path.join(self.seed, "f"), "w") as fh:
            fh.write("one\n")
        git("-C", self.seed, "add", "f")
        git("-C", self.seed, "commit", "-q", "-m", "f")
        git("-C", self.seed, "push", "-q", "origin", "main")
        git("-C", self.clone, "fetch", "-q")

    def link_commit(self):
        os.symlink("/etc/passwd", os.path.join(self.seed, "link"))
        git("-C", self.seed, "add", "link")
        git("-C", self.seed, "commit", "-q", "-m", "link")
        git("-C", self.seed, "push", "-q", "origin", "main")
        return os.path.join(self.wt, "link")

    def assertGood(self):
        index = git("-C", self.wt, "rev-parse", "--path-format=absolute", "--git-path", "index")
        self.assertTrue(os.path.isfile(index))
        self.assertTrue(os.path.isfile(os.path.join(self.wt, "f")))
        self.assertEqual(git("-C", self.wt, "status", "--porcelain"), "")
        self.assertEqual(
            git("-C", self.wt, "config", "--worktree", "--get", "core.symlinks"), "false"
        )

    def symlinks(self, wt=None):
        """Worktree `wt`'s own core.symlinks, else the clone's; None when unset."""
        res = git_run(
            *[
                "-C",
                wt or self.clone,
                "config",
                *(["--worktree"] if wt else []),
                "--get",
                "core.symlinks",
            ],
        )
        return res.stdout.strip() if res.returncode == 0 else None

    def remove(self):
        shutil.rmtree(self.wt, ignore_errors=True)
        git("-C", self.clone, "worktree", "prune")

    def half_made(self):
        """A worktree whose checkout never finished."""
        git("-C", self.clone, "config", "extensions.worktreeConfig", "true")
        git(
            "-C",
            self.clone,
            "worktree",
            "add",
            "-q",
            "--no-checkout",
            "-b",
            "TASK-1-x",
            self.wt,
            "origin/main",
        )

    def test_a_local_clone_under_a_temp_dir_is_refused(self):
        code, err = self.worktree(temp=(self.tmp,))
        self.assertEqual(code, 2)
        self.assertIn("temp dir", err)
        self.assertFalse(os.path.exists(self.wt))

    def test_a_failed_step_after_the_add_leaves_no_worktree_and_a_rerun_succeeds(self):
        self.file_commit()
        for step, fail_on in (
            ("config", lambda a: a[3:5] == ["config", "--worktree"] and "--get" not in a),
            ("reset", lambda a: a[3:4] == ["reset"]),
        ):
            with self.subTest(step):
                code, err = self.worktree(fail_on=fail_on)
                self.assertEqual(code, 1, err)
                self.assertFalse(os.path.exists(self.wt))
                self.assertNotIn(self.wt, git("-C", self.clone, "worktree", "list", "--porcelain"))
                self.assertEqual(self.worktree(), (0, ""))
                self.assertGood()
                self.remove()

    def test_a_half_made_worktree_is_made_again(self):
        self.file_commit()
        self.half_made()
        self.assertEqual(self.worktree(), (0, ""))
        self.assertGood()

    def test_a_half_made_worktree_of_a_listed_clone_is_made_again_with_symlinks(self):
        link = self.link_commit()
        git("-C", self.clone, "fetch", "-q")
        self.half_made()
        self.assertEqual(self.worktree(config=self.listing(self.clone)), (0, ""))
        self.assertEqual(
            git("-C", self.wt, "config", "--worktree", "--get", "core.symlinks"), "true"
        )
        self.assertEqual(os.readlink(link), "/etc/passwd")

    def test_only_a_listed_clones_worktree_gets_symlinks_and_the_clone_keeps_its_config(self):
        """trusted_dirs lists clones by real path, in the config or its local file; listing another
        clone of the same origin trusts no other.
        """
        link = self.link_commit()
        other, alias = os.path.join(self.tmp, "clone2"), os.path.join(self.tmp, "alias")
        git("clone", "-q", self.bare, other)
        os.symlink(self.clone, alias)
        local = os.path.join(self.tmp, "config.local.toml")
        for case, listed, in_local, spec, on in (
            ("none listed", None, False, None, False),
            ("listed", [self.clone], False, None, True),
            ("another clone of the origin listed", [other], False, None, False),
            ("listed by an alias", [alias], False, None, True),
            ("listed, given by an alias", [self.clone], False, alias, True),
            ("listed in the local file", [], True, None, True),
        ):
            with self.subTest(case):
                if in_local:
                    with open(local, "w") as f:
                        f.write(f"[delegate]\ntrusted_dirs = {json.dumps([self.clone])}\n")
                try:
                    config = None if listed is None else self.listing(*listed)
                    self.assertEqual(self.worktree(config=config, spec=spec, local=local), (0, ""))
                    if on:
                        self.assertEqual(os.readlink(link), "/etc/passwd")
                    else:
                        self.assertTrue(os.path.isfile(link) and not os.path.islink(link))
                    for scope in (["--worktree"], []):  # set per worktree, and so in effect
                        self.assertEqual(
                            git("-C", self.wt, "config", *scope, "--get", "core.symlinks"),
                            str(on).lower(),
                        )
                    self.assertEqual(git("-C", self.wt, "status", "--porcelain"), "")
                    self.assertIsNone(self.symlinks())
                    self.assertEqual(git("-C", self.clone, "status", "--porcelain"), "")
                finally:
                    self.remove()
                    if in_local:
                        os.remove(local)

    def test_an_existing_worktree_must_have_the_listed_setting(self):
        """Made by git (unset), unlisted (false) or listed (true): run with the other setting,
        refused and unchanged.
        """
        self.file_commit()
        listed = self.listing(self.clone)
        for made, run_config, found, gives in (
            ("by git", None, "unset", "false"),
            ("unlisted", listed, "false", "true"),
            ("listed", None, "true", "false"),
        ):
            with self.subTest(made=made):
                if made == "by git":
                    git(
                        "-C",
                        self.clone,
                        "worktree",
                        "add",
                        "-q",
                        "-b",
                        "TASK-1-x",
                        self.wt,
                        "origin/main",
                    )
                else:
                    self.assertEqual(
                        self.worktree(config=listed if made == "listed" else None), (0, "")
                    )
                code, err = self.worktree(config=run_config)
                self.assertEqual(code, 2, err)
                for text in (
                    f"core.symlinks={found}",
                    f"trusted_dirs gives {gives}",
                    "remove it and run again",
                ):
                    self.assertIn(text, err)
                self.assertEqual(self.symlinks(self.wt), None if found == "unset" else found)
                self.remove()
        self.assertEqual(self.worktree(config=listed), (0, ""))
        self.assertEqual(self.worktree(config=listed), (0, ""))

    def test_a_worktree_deleted_by_hand_is_added_again(self):
        other = os.path.join(self.tmp, "users-own")
        git("-C", self.clone, "worktree", "add", "-q", "-b", "mine", other)
        shutil.rmtree(other)
        self.assertEqual(self.worktree()[0], 0)
        shutil.rmtree(self.wt)
        self.assertEqual(self.worktree(), (0, ""))
        self.assertEqual(git("-C", self.wt, "branch", "--show-current"), "TASK-1-x")
        self.assertIn(f"worktree {other}", git("-C", self.clone, "worktree", "list", "--porcelain"))

    def test_a_mirror_refspec_cannot_move_the_clones_branches(self):
        git("-C", self.clone, "branch", "TASK-9-mine")
        mine = git("-C", self.clone, "rev-parse", "TASK-9-mine")
        git("-C", self.seed, "commit", "-q", "--allow-empty", "-m", "theirs")
        git("-C", self.seed, "push", "-q", "origin", "HEAD:TASK-9-mine")
        git("-C", self.clone, "config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*")
        self.assertEqual(self.worktree()[0], 0)
        self.assertEqual(git("-C", self.clone, "rev-parse", "TASK-9-mine"), mine)
