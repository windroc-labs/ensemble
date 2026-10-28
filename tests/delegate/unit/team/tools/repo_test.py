"""Repository tool decisions over fake command output."""

import io
import json
import os
import shutil
import unittest
import unittest.mock

from tests.delegate.case import Case
from tests.delegate.repo import (
    FETCH,
    HEAD,
    LOCAL,
    LOGIN,
    NO_FEEDBACK,
    PROCESS_ERRORS,
    REPO_API,
    SHA,
    Base,
    Fake,
    done,
    fail,
    info,
    ok,
    pr,
    pr_list,
    repo,
)
from tests.utils.sandbox import scratch


def dry_push(argv):
    return argv[3:5] == ["push", "--dry-run"]


class CommandFailures(Base):
    def test_process_failures_are_reported_with_exit_one(self):
        for error in PROCESS_ERRORS:
            with self.subTest(error=error):

                def run(_argv, _timeout, error=error):
                    raise error

                code, out, err = self.main(
                    ["worktree", "o/n", "--dir", self.dir, "--branch", "TASK-1-x"], run
                )
                self.assertEqual((code, out), (1, ""))
                self.assertIn(str(error), err)

    def test_unexpected_runner_defects_are_not_reported_as_command_failures(self):
        for error in (AttributeError("missing result"), TypeError("bad call"), RuntimeError("bug")):
            with self.subTest(error=error):

                def run(_argv, _timeout, error=error):
                    raise error

                with self.assertRaises(type(error)) as raised:
                    self.main(["worktree", "o/n", "--dir", self.dir, "--branch", "TASK-1-x"], run)
                self.assertIs(raised.exception, error)


class MalformedGh(Base):
    def test_malformed_repository_data_gets_one_redacted_command_error_line(self):
        run = Fake(
            [
                (
                    ["gh", "api"],
                    ok(json.dumps({"permissions": {"push": []}, "default_branch": "main"})),
                )
            ]
        )
        code, out, err = self.main(
            ["worktree", "o/n", "--dir", self.dir, "--branch", "TASK-1-x"], run
        )
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "repo.py: gh api repos/o/n: invalid repository fields\n")
        self.assertFalse(run.ran("gh", "repo", "clone"))


class PlanDocs(Case):
    def test_only_updraft_plan_docs_of_the_branch_count_at_a_step_or_done_newest_last(self):
        """Updraft resumes only from <worktree>/.updraft/; a copy elsewhere is not a plan doc.
        Updraft rewrites a plan doc's RESUME at every phase change, so the newest is the last touched.
        """
        wt = scratch(self, real=False)

        def doc(path, resume):
            path = os.path.join(wt, path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(f"Rules: x\nRESUME: {resume}\n")
            return path

        done = doc(".updraft/a-plan.md", "phase=done worktree=/w branch=b")
        open_ = doc(".updraft/b-plan.md", "phase=S5 worktree=/w branch=b")
        doc(".updraft/c-plan.md", "phase=S5 worktree=/w branch=other")
        doc(".updraft/a-spec.md", "phase=S5 worktree=/w branch=b")
        doc("docs/a-plan.md", "phase=S7 worktree=/w branch=b")
        os.utime(open_, (1_000, 1_000))
        os.utime(done, (2_000, 2_000))
        self.assertEqual(
            repo.plan_docs(wt, "b"),
            [{"path": open_, "phase": "S5"}, {"path": done, "phase": "done"}],
        )


class Status(Base):
    def plan_doc(self, branch="TASK-1-x", phase="S5"):
        os.makedirs(os.path.join(self.wt, ".updraft"), exist_ok=True)
        path = os.path.join(self.wt, ".updraft", "x-plan.md")
        with open(path, "w") as f:
            f.write(f"# Plan\n\nRESUME: phase={phase} branch={branch}\n")
        return path

    def status(self, *answers, argv=(), config=None, local=None):
        """(exit code, the output or stderr, run) of status on a checkout of o/n; `answers` after
        the gh login's.
        """
        run = Fake([self.existing(), LOGIN, *answers])
        code, out, err = self.main(
            ["status", "o/n", "--branch", "TASK-1-x", "--dir", self.dir, *argv], run, config, local
        )
        return code, json.loads(out) if code == 0 else err, run

    def test_pr_plan_docs_and_feedback_since_its_latest_own_commit(self):
        path = self.plan_doc()
        comments = [
            [
                {"created_at": "2026-10-01T00:00:00Z", "user": {"login": "me"}, "body": "old"},
                {"created_at": "2026-10-03T00:00:00Z", "user": {"login": "me"}, "body": "do Y"},
            ]
        ]
        reviews = [
            [
                {
                    "submitted_at": "2026-10-03T01:00:00Z",
                    "user": {"login": "bot"},
                    "body": "nit",
                    "state": "COMMENTED",
                },
                {"submitted_at": None, "user": {"login": "bot"}, "body": "pending"},
            ]
        ]
        code, r, run = self.status(
            (
                lambda a: (
                    a[3:]
                    == [
                        "log",
                        "-1",
                        "--first-parent",
                        "--no-merges",
                        "--format=%cI",
                        "origin/TASK-0-a..HEAD",
                    ]
                ),
                ok("2026-10-02T00:00:00+00:00\n"),
            ),
            pr_list(
                [
                    {
                        "number": 9,
                        "url": "u9",
                        "state": "OPEN",
                        "isCrossRepository": True,
                        "author": {"login": "me"},
                    },
                    pr("TASK-0-a"),
                ]
            ),
            (lambda a: a[-1].endswith("/issues/7/comments"), ok(json.dumps(comments))),
            (lambda a: a[-1].endswith("/pulls/7/reviews"), ok(json.dumps(reviews))),
            (lambda a: a[-1].endswith("/pulls/7/comments"), ok("[[]]")),
        )
        self.assertEqual(code, 0, r)
        self.assertEqual(
            r["pr"],
            {"number": 7, "url": "u7", "state": "OPEN", "baseRefName": "TASK-0-a", "isDraft": True},
        )
        self.assertIn(
            "number,url,state,baseRefName,isDraft,isCrossRepository,author,mergeCommit",
            next(c for c in run.calls if c[1] == "pr"),
        )
        self.assertEqual(r["plan_docs"], [{"path": path, "phase": "S5"}])
        self.assertEqual(r["since"], "2026-10-02T00:00:00+00:00")
        self.assertEqual([e["body"] for e in r["user"]], ["do Y"])
        self.assertEqual([(e["body"], e["state"]) for e in r["others"]], [("nit", "COMMENTED")])

    def test_a_merged_prs_own_commits_are_those_off_its_base_before_the_merge(self):
        """A merge commit puts B's commits on origin/<base>, so they count up to its first parent;
        a merge commit id that is no SHA falls back to origin/<base>.
        """
        comments = [
            [
                {"created_at": "2026-10-01T00:00:00Z", "user": {"login": "me"}, "body": "old"},
                {"created_at": "2026-10-03T00:00:00Z", "user": {"login": "me"}, "body": "do Y"},
            ]
        ]
        merged = "b" * 40
        for oid, excluded in (
            (merged, f"{merged}^1"),
            ("--all", "origin/main"),
            (None, "origin/main"),
        ):
            with self.subTest(oid=oid):
                row = {**pr(), "state": "MERGED", "mergeCommit": oid and {"oid": oid}}
                code, r, _ = self.status(
                    (
                        lambda a, excluded=excluded: (
                            a[3:]
                            == [
                                "log",
                                "-1",
                                "--first-parent",
                                "--no-merges",
                                "--format=%cI",
                                f"{excluded}..HEAD",
                            ]
                        ),
                        ok("2026-10-02T00:00:00+00:00\n"),
                    ),
                    pr_list([row]),
                    (lambda a: a[-1].endswith("/issues/7/comments"), ok(json.dumps(comments))),
                    NO_FEEDBACK,
                )
                self.assertEqual(code, 0, r)
                self.assertEqual(r["since"], "2026-10-02T00:00:00+00:00")
                self.assertEqual([e["body"] for e in r["user"]], ["do Y"])

    def test_a_pr_branch_without_own_commits_filters_no_feedback(self):
        comments = [
            [{"created_at": "2026-10-01T00:00:00Z", "user": {"login": "me"}, "body": "old"}]
        ]
        code, r, _ = self.status(
            pr_list([pr()]),
            (lambda a: a[-1].endswith("/issues/7/comments"), ok(json.dumps(comments))),
            NO_FEEDBACK,
        )
        self.assertEqual(code, 0, r)
        self.assertIsNone(r["since"])
        self.assertEqual([e["body"] for e in r["user"]], ["old"])

    def test_pr_says_whether_it_is_a_draft(self):
        comments = [[]]
        for draft in (True, False):
            with self.subTest(draft=draft):
                code, r, _ = self.status(
                    pr_list([pr(draft=draft)]),
                    (lambda a: a[-1].endswith("/issues/7/comments"), ok(json.dumps(comments))),
                    NO_FEEDBACK,
                )
                self.assertEqual((code, r["pr"]["isDraft"]), (0, draft), r)

    def test_configured_users_replace_the_gh_login(self):
        """From the config and local passed only, never the HOME one (malformed here)."""
        comments = [
            [
                {
                    "created_at": "2026-10-03T00:00:00Z",
                    "user": {"login": "me"},
                    "body": "from the bot",
                },
                {"created_at": "2026-10-03T01:00:00Z", "user": {"login": "Alice"}, "body": "do Y"},
            ]
        ]
        with open(os.path.join(self.ensemble, LOCAL), "w") as f:
            f.write("malformed = [\n")
        local = os.path.join(self.dir, "config.local.toml")
        with open(local, "w") as f:
            f.write('[delegate]\nusers = ["alice"]\n')
        for text, given in (('users = ["alice"]\n', os.devnull), ('users = ["bob"]\n', local)):
            with self.subTest(local=given):
                code, r, _ = self.status(
                    pr_list([pr()]),
                    (lambda a: a[-1].endswith("/issues/7/comments"), ok(json.dumps(comments))),
                    NO_FEEDBACK,
                    config=self.config(text),
                    local=given,
                )
                self.assertEqual((code, r["pr"]["number"]), (0, 7), r)
                self.assertEqual([e["body"] for e in r["user"]], ["do Y"])
                self.assertEqual([e["body"] for e in r["others"]], ["from the bot"])
        with self.subTest("users = [] is the gh login"):
            code, r, _ = self.status(
                pr_list([pr()]),
                (lambda a: a[-1].endswith("/issues/7/comments"), ok(json.dumps(comments))),
                NO_FEEDBACK,
                config=self.config("users = []\n"),
                local=os.devnull,
            )
            self.assertEqual((code, [e["body"] for e in r["user"]]), (0, ["from the bot"]), r)
            self.assertEqual([e["body"] for e in r["others"]], ["do Y"])

    def test_the_pr_author_and_users_from_the_environment_replace_the_gh_login(self):
        comments = [
            [
                {
                    "created_at": f"2026-10-03T0{i}:00:00Z",
                    "user": {"login": login},
                    "body": login,
                }
                for i, login in enumerate(
                    ("ensemble-bot[bot]", "Alice", "me", "bob", "helper[bot]")
                )
            ]
        ]
        bot = {**pr(), "author": {"is_bot": True, "login": "app/ensemble-bot"}}
        for users, want in (
            ('["alice", "helper[bot]", "ensemble-bot[bot]"]', ["Alice"]),
            ("[]", []),
        ):
            env = {"ENSEMBLE_PR_AUTHOR": "ensemble-bot[bot]", "ENSEMBLE_USERS": users}
            with self.subTest(users=users), unittest.mock.patch.dict(os.environ, env):
                code, r, run = self.status(
                    pr_list([{**pr(), "number": 9, "url": "u9"}, bot]),
                    (lambda a: a[-1].endswith("/issues/7/comments"), ok(json.dumps(comments))),
                    NO_FEEDBACK,
                    config=self.config('users = ["bob"]\n'),
                )
                self.assertEqual((code, r["pr"]["number"]), (0, 7), r)
                self.assertEqual([e["body"] for e in r["user"]], want)
                self.assertEqual(
                    [e["body"] for e in r["others"]],
                    [c["body"] for c in comments[0] if c["body"] not in want],
                )
                self.assertFalse(run.ran("gh", "api", "--hostname", "github.com", "user"))

    def test_without_a_pr_the_app_reads_the_default_branch_without_permissions(self):
        with unittest.mock.patch.dict(os.environ, {"ENSEMBLE_PR_AUTHOR": "ensemble-bot[bot]"}):
            code, r, _ = self.status(
                pr_list([]), ([*REPO_API], ok(json.dumps({"default_branch": "trunk"})))
            )
        self.assertEqual((code, r["pr"], r["base"]), (0, None, "trunk"), r)

    def test_users_from_the_environment_replace_the_configured_ones_on_the_host(self):
        comments = [
            [
                {"created_at": "2026-10-03T00:00:00Z", "user": {"login": "bob"}, "body": "bob"},
                {"created_at": "2026-10-03T01:00:00Z", "user": {"login": "alice"}, "body": "alice"},
            ]
        ]
        with unittest.mock.patch.dict(os.environ, {"ENSEMBLE_USERS": '["alice"]'}):
            code, r, run = self.status(
                pr_list([pr()]),
                (lambda a: a[-1].endswith("/issues/7/comments"), ok(json.dumps(comments))),
                NO_FEEDBACK,
                config=self.config('users = ["bob"]\n'),
            )
        self.assertEqual((code, r["pr"]["number"]), (0, 7), r)
        self.assertEqual([e["body"] for e in r["user"]], ["alice"])
        self.assertEqual([e["body"] for e in r["others"]], ["bob"])
        self.assertTrue(run.ran("gh", "api", "--hostname", "github.com", "user"))

    def test_an_invalid_ensemble_users_is_reported_before_the_repo_is_read(self):
        for value in ("[1]", "{}", '"me"', "not json", ""):
            with (
                self.subTest(value=value),
                unittest.mock.patch.dict(os.environ, {"ENSEMBLE_USERS": value}),
            ):
                run = Fake([])
                code, out, err = self.main(
                    ["status", "--dir", self.dir, "--branch", "b", "not a repo"], run
                )
                self.assertEqual((code, out), (1, ""))
                self.assertEqual(err, "repo.py: ENSEMBLE_USERS: want a JSON array of strings\n")
                self.assertEqual(run.calls, [])

    def test_other_branches_plan_docs_and_no_pr(self):
        self.plan_doc(branch="other")
        self.assertEqual(
            self.status(pr_list([]), info())[:2],
            (
                0,
                {
                    "pr": None,
                    "base": "main",
                    "plan_docs": [],
                    "since": None,
                    "user": [],
                    "others": [],
                },
            ),
        )

    def test_an_invalid_trusted_dirs_does_not_affect_status(self):
        code, err, _ = self.status(
            pr_list([]), info(), config=self.config('trusted_dirs = ["a/b"]\n')
        )
        self.assertEqual(code, 0, err)

    def test_bad_config_is_reported_before_an_unreadable_repo(self):
        code, _, _ = self.main(
            ["status", "--dir", self.dir, "--branch", "b", "not a repo"],
            Fake([]),
            self.config("users = [\n"),
        )
        self.assertEqual(code, 1)

    def test_invalid_users_are_reported_before_the_repo_is_read(self):
        for value in ("[1]", "5", '["me", 1]', '"me"'):
            with self.subTest(users=value):
                run = Fake([])
                code, out, err = self.main(
                    ["status", "--dir", self.dir, "--branch", "b", "not a repo"],
                    run,
                    self.config(f"users = {value}\n"),
                )
                self.assertEqual((code, out), (1, ""))
                self.assertEqual(err, "repo.py: users: want a list of strings\n")
                self.assertEqual(run.calls, [])

    def test_the_defaults_load_the_local_file(self):
        with open(os.path.join(self.ensemble, "config.local.toml"), "w") as stream:
            stream.write("users = [\n")
        errors = io.StringIO()
        code = repo.main(
            ["status", "o/n", "--branch", "b", "--dir", self.dir],
            run=Fake([]),
            out=io.StringIO(),
            err=errors,
        )
        self.assertEqual(code, 1)
        self.assertIn("Invalid value", errors.getvalue())

    def test_needs_a_checkout(self):
        code, _, err = self.main(["status", "o/n", "--branch", "b", "--dir", self.dir], Fake([]))
        self.assertEqual(code, 2)
        self.assertIn("run worktree first", err)

    def test_base_is_the_prs_else_the_option_else_the_default(self):
        for prs, argv, want, asks in (
            ([pr("TASK-0-a")], ["--base", "TASK-0-b"], "TASK-0-a", False),
            ([], ["--base", "TASK-0-b"], "TASK-0-b", False),
            ([], [], "trunk", True),
        ):
            with self.subTest(prs=prs, argv=argv):
                code, r, run = self.status(
                    pr_list(prs), NO_FEEDBACK, info(default="trunk"), argv=argv
                )
                self.assertEqual((code, r["base"]), (0, want), r)
                self.assertEqual(run.ran(*REPO_API), asks)

    def test_an_unsafe_base_exits_2(self):
        for prs, argv, default in (
            ([pr("a..b")], [], "main"),
            ([pr(None)], [], "main"),
            ([], ["--base", "x;rm -rf ~"], "main"),
            ([], ["--base", ""], "main"),
            ([], [], "-x"),
        ):
            with self.subTest(prs=prs, argv=argv, default=default):
                code, err, _ = self.status(
                    pr_list(prs), NO_FEEDBACK, info(default=default), argv=argv
                )
                self.assertEqual(code, 2, err)
                self.assertIn("unsafe base branch name", err)

    def test_malformed_pull_requests_are_one_line_command_errors(self):
        for payload in ({}, [None]):
            with self.subTest(payload=payload):
                code, error, _ = self.status(pr_list(payload))
                self.assertEqual(code, 1)
                self.assertTrue(error.startswith("repo.py: gh"), error)
                self.assertEqual(len(error.splitlines()), 1)

    def test_missing_author_logins_are_ignored_for_pull_requests(self):
        for author in (None, {}, "deleted", [], {"login": ""}, {"login": 7}):
            with self.subTest(author=author):
                code, result, _ = self.status(pr_list([{**pr(), "author": author}]), info())
                self.assertEqual(code, 0, result)
                self.assertIsNone(result["pr"])

    def test_missing_feedback_logins_are_anonymous(self):
        for author in (None, {}, "deleted", [], {"login": ""}):
            with self.subTest(author=author):
                comment = {
                    "created_at": "2026-10-03T00:00:00Z",
                    "user": author,
                    "body": "feedback",
                }
                code, result, _ = self.status(
                    pr_list([pr()]),
                    (lambda a: a[-1].endswith("/issues/7/comments"), ok(json.dumps([[comment]]))),
                    NO_FEEDBACK,
                )
                self.assertEqual(code, 0, result)
                self.assertEqual(result["user"], [])
                self.assertEqual(len(result["others"]), 1)
                expected = author.get("login") if isinstance(author, dict) else None
                self.assertEqual(result["others"][0]["author"], expected)

    def test_feedback_author_logins_keep_their_values_without_normalization(self):
        def feedback(login):
            comment = {
                "created_at": "2026-10-03T00:00:00Z",
                "user": {"login": login},
                "body": "feedback",
            }
            return self.status(
                pr_list([pr()]),
                (lambda a: a[-1].endswith("/issues/7/comments"), ok(json.dumps([[comment]]))),
                NO_FEEDBACK,
            )

        for login in (None, "", []):
            with self.subTest(login=login):
                code, result, _ = feedback(login)
                self.assertEqual(code, 0, result)
                self.assertEqual(result["others"][0]["author"], login)
                self.assertIs(type(result["others"][0]["author"]), type(login))
        for login in ({"malformed": "login"},):
            with self.subTest(login=login), self.assertRaises(AttributeError):
                feedback(login)

    def test_a_non_string_feedback_login_keeps_main_failure(self):
        comment = {"created_at": "2026-10-03T00:00:00Z", "user": {"login": 7}, "body": "feedback"}
        with self.assertRaises(AttributeError):
            self.status(
                pr_list([pr()]),
                (lambda a: a[-1].endswith("/issues/7/comments"), ok(json.dumps([[comment]]))),
                NO_FEEDBACK,
            )

    def test_malformed_comments_are_one_line_command_errors(self):
        for payload in (
            [None],
            [[None]],
            [[{"created_at": []}]],
            [[{"created_at": "invalid"}]],
            [[{"created_at": "2026-01-01"}]],
        ):
            with self.subTest(payload=payload):
                code, error, _ = self.status(
                    pr_list([pr()]), (lambda a: "--paginate" in a, ok(json.dumps(payload)))
                )
                self.assertEqual(code, 1)
                self.assertTrue(error.startswith("repo.py: gh"), error)
                self.assertEqual(len(error.splitlines()), 1)


class Worktree(Base):
    def worktree(self, *answers, branch="TASK-1-x", spec="o/n", config=None, argv=()):
        run = Fake([*answers, HEAD])
        return self.main(
            ["worktree", spec, f"--branch={branch}", "--dir", self.dir, *argv], run, config
        ), run

    def test_new_clone_branch_starts_from_the_explicit_base(self):
        (code, _, err), run = self.worktree(info(), argv=["--base", "re-architecture"])
        self.assertEqual(code, 0, err)
        self.assertTrue(
            run.ran(
                "git",
                "-C",
                self.wt,
                "checkout",
                "--no-track",
                "-b",
                "TASK-1-x",
                "origin/re-architecture",
            )
        )

    def test_explicit_base_preserves_existing_clone_branch_precedence(self):
        for local in (True, False):
            with self.subTest(local=local):
                (code, _, err), run = self.worktree(
                    self.existing(),
                    info(),
                    (lambda a: a[3:5] == ["branch", "--list"], ok("TASK-1-x" if local else "")),
                    (lambda a: "ls-remote" in a, ok("abc\trefs/heads/TASK-1-x\n")),
                    argv=["--base", "missing"],
                )
                self.assertEqual(code, 0, err)
                options = [] if local else ["--track", "-b", "TASK-1-x"]
                start = "TASK-1-x" if local else "origin/TASK-1-x"
                self.assertTrue(run.ran("git", "-C", self.wt, "checkout", *options, start))
                self.assertEqual(run.ran("git", "-C", self.wt, "ls-remote"), not local)

    def test_unsafe_base_is_refused_before_fetch_or_clone(self):
        for base in ("-x", "a..b", "x;rm -rf ~", "", "x/", "a" * 101):
            with self.subTest(base=base):
                (code, _, err), run = self.worktree(info(), argv=[f"--base={base}"])
                self.assertEqual(code, 2, err)
                self.assertIn("unsafe base branch name", err)
                self.assertFalse(
                    any("fetch" in c or "clone" in c or "checkout" in c for c in run.calls)
                )

    def test_missing_clone_base_reports_the_git_failure(self):
        (code, _, err), _ = self.worktree(
            info(),
            (lambda a: "checkout" in a, fail("fatal: 'origin/missing' is not a commit")),
            argv=["--base", "missing"],
        )
        self.assertEqual(code, 1, err)
        self.assertIn("origin/missing", err)

    def test_fresh_blobless_clone_on_a_new_branch_from_the_default(self):
        (code, out, _), run = self.worktree(info(default="trunk"))
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(out),
            {
                "repo": "o/n",
                "host": "github.com",
                "default": "trunk",
                "branch": "TASK-1-x",
                "commit": SHA,
                "worktree": self.wt,
                "permalink_base": f"https://github.com/o/n/blob/{SHA}/",
                "push": True,
            },
        )
        self.assertIn(
            [
                "gh",
                "repo",
                "clone",
                "github.com/o/n",
                self.wt,
                "--",
                "-c",
                "core.symlinks=false",
                "--filter=blob:none",
            ],
            run.calls,
        )
        self.assertTrue(
            run.ran(
                "git", "-C", self.wt, "checkout", "--no-track", "-b", "TASK-1-x", "origin/trunk"
            )
        )
        self.assertEqual(run.timeout("git", "-C", self.wt, "checkout"), repo.LONG)

    def test_a_remote_repo_gets_no_symlinks_whatever_the_list(self):
        (code, _, err), run = self.worktree(
            info(), config=self.config(f'trusted_dirs = ["{self.dir}", "~/n"]\n')
        )
        self.assertEqual(code, 0, err)
        self.assertIn(
            [
                "gh",
                "repo",
                "clone",
                "github.com/o/n",
                self.wt,
                "--",
                "-c",
                "core.symlinks=false",
                "--filter=blob:none",
            ],
            run.calls,
        )

    def test_a_bad_trusted_dirs_fails_worktree_before_the_repo_is_read(self):
        for text, want in (
            ("trusted_dirs = [\n", "(at "),
            ('trusted_dirs = "/a"\n', "trusted_dirs"),
            ("trusted_dirs = [1]\n", "trusted_dirs"),
            ('trusted_dirs = ["a/b"]\n', "trusted_dirs"),
            ('trusted_dirs = ["~nosuchuser/x"]\n', "trusted_dirs"),
        ):
            with self.subTest(text=text):
                run = Fake([])
                code, _, err = self.main(
                    ["worktree", "--dir", self.dir, "--branch", "TASK-1-x", "not a repo"],
                    run,
                    self.config(text),
                )
                self.assertEqual((code, run.calls), (1, []), err)
                self.assertIn(want, err)

    def test_remote_branch_is_tracked(self):
        (code, _, _), run = self.worktree(
            info(), (lambda a: "ls-remote" in a, ok("abc\trefs/heads/TASK-1-x\n"))
        )
        self.assertEqual(code, 0)
        self.assertTrue(
            run.ran("git", "-C", self.wt, "ls-remote", "--heads", "origin", "refs/heads/TASK-1-x")
        )
        self.assertTrue(
            run.ran(
                "git", "-C", self.wt, "checkout", "--track", "-b", "TASK-1-x", "origin/TASK-1-x"
            )
        )
        self.assertEqual(run.timeout("git", "-C", self.wt, "checkout"), repo.LONG)

    def test_default_branch_is_invalid(self):
        (code, _, err), run = self.worktree(info(default="main"), branch="main")
        self.assertEqual(code, 2)
        self.assertIn("default branch", err)
        self.assertFalse(run.ran("gh", "repo", "clone"))

    def test_no_push_permission_is_reported_not_refused(self):
        (code, out, _), run = self.worktree(info(push=False))
        self.assertEqual((code, json.loads(out)["push"]), (0, False))
        self.assertTrue(run.ran("gh", "repo", "clone"))

    def test_a_repo_naming_no_push_permission_is_malformed_for_a_user(self):
        for data in ({"default_branch": "main"}, {"permissions": {}, "default_branch": "main"}):
            with self.subTest(data=data):
                (code, out, err), _ = self.worktree(([*REPO_API], ok(json.dumps(data))))
                self.assertEqual((code, out), (1, ""))
                self.assertIn("gh api repos/o/n", err)

    def app_worktree(self, *answers):
        with unittest.mock.patch.dict(os.environ, {"ENSEMBLE_PR_AUTHOR": "ensemble-bot[bot]"}):
            return self.worktree(*answers)

    def test_the_app_pushes_when_a_dry_run_push_reaches_the_repo(self):
        """An App installation token gets every permission false, `pull` included."""
        none = dict.fromkeys(("admin", "maintain", "pull", "push", "triage"), False)
        for data in (
            {"default_branch": "main"},
            {"permissions": {}, "default_branch": "main"},
            {"permissions": none, "default_branch": "main"},
        ):
            with self.subTest(data=data):
                (code, out, err), run = self.app_worktree(([*REPO_API], ok(json.dumps(data))))
                self.assertEqual((code, json.loads(out)["push"]), (0, True), err)
                self.assertTrue(run.ran("gh", "repo", "clone"))
                self.assertIn(
                    [
                        "git",
                        "-C",
                        self.wt,
                        "push",
                        "--dry-run",
                        "--no-verify",
                        "--porcelain",
                        "origin",
                        "HEAD:refs/heads/TASK-1-x",
                    ],
                    run.calls,
                )
                self.assertEqual(run.timeout("git", "-C", self.wt, "push"), repo.SHORT)
                shutil.rmtree(self.wt)

    def test_a_rejected_ref_still_shows_the_app_can_push(self):
        rejected = done(
            1,
            "To https://github.com/o/n.git\n!\tHEAD:refs/heads/TASK-1-x\t[rejected] "
            "(non-fast-forward)\nDone\n",
            "error: failed to push some refs\n",
        )
        (code, out, err), _ = self.app_worktree(info(push=False), (dry_push, rejected))
        self.assertEqual((code, json.loads(out)["push"]), (0, True), err)

    def test_the_app_cannot_push_where_github_or_git_denies_it(self):
        """The App not installed on a public repo: GitHub answers 403; no token for the owner: git
        finds no credentials."""
        for stderr in (
            "remote: Permission to o/n.git denied to ensemble-bot[bot].\nfatal: unable to access "
            "'https://github.com/o/n.git/': The requested URL returned error: 403\n",
            "fatal: could not read Username for 'https://github.com': No such device or address\n",
            "fatal: could not read Username for 'https://github.com': terminal prompts disabled\n",
            "fatal: Authentication failed for 'https://github.com/o/n.git/'\n",
            "remote: Write access to repository not granted.\nfatal: unable to access "
            "'https://github.com/o/n.git/': The requested URL returned error: 403\n",
        ):
            with self.subTest(stderr=stderr):
                (code, out, err), run = self.app_worktree(info(), (dry_push, done(128, "", stderr)))
                self.assertEqual((code, json.loads(out)["push"]), (0, False), err)
                self.assertTrue(run.ran("gh", "repo", "clone"))
                shutil.rmtree(self.wt)

    def test_another_dry_run_push_failure_is_a_command_error(self):
        """A repo or path naming `denied` or `403` is no denial."""
        for url in ("https://x:secret@github.com/o/n.git/", "https://github.com/o/denied-403.git/"):
            with self.subTest(url=url):
                failed = done(
                    128,
                    "",
                    f"fatal: unable to access '{url}': Could not resolve host: github.com\n",
                )
                (code, out, err), _ = self.app_worktree(info(), (dry_push, failed))
                self.assertEqual((code, out), (1, ""))
                self.assertIn("git push --dry-run", err)
                self.assertIn("Could not resolve host", err)
                self.assertNotIn("secret", err)
                shutil.rmtree(self.wt)

    def test_a_user_token_never_runs_a_dry_run_push(self):
        for push in (True, False):
            with self.subTest(push=push):
                (code, out, err), run = self.worktree(info(push=push))
                self.assertEqual((code, json.loads(out)["push"]), (0, push), err)
                self.assertFalse(run.ran("git", "-C", self.wt, "push"))
                shutil.rmtree(self.wt)

    def test_reuse_fetches_and_switches_to_the_local_branch(self):
        (code, _, _), run = self.worktree(
            self.existing(), info(), (lambda a: a[3:5] == ["branch", "--list"], ok("  TASK-1-x\n"))
        )
        self.assertEqual(code, 0)
        self.assertTrue(run.ran("git", "-C", self.wt, *FETCH))
        self.assertTrue(run.ran("git", "-C", self.wt, "checkout", "TASK-1-x"))
        self.assertFalse(run.ran("gh", "repo", "clone"))

    def test_already_on_the_branch_changes_nothing(self):
        (code, _, _), run = self.worktree(
            self.existing(),
            info(),
            (lambda a: "--show-current" in a, ok("TASK-1-x\n")),
            argv=["--base", "re-architecture"],
        )
        self.assertEqual(code, 0)
        self.assertFalse(any("checkout" in c or "worktree" in c for c in run.calls))

    def test_the_checkout_is_named_by_owner_and_name_else_by_the_branch(self):
        """One --dir: each row's checkout lands beside the earlier ones (the same name under two
        owners included).
        """
        for spec, branch, argv, path in (
            ("a/n", "TASK-1-x", [], "a/n-TASK-1-x"),
            ("b/n", "TASK-1-x", [], "b/n-TASK-1-x"),
            ("o/n", "feat/x", [], "o/n-feat-x"),
            ("o/n", "feat/x", ["--name", "TASK-1"], "o/n-TASK-1"),
        ):
            with self.subTest(spec=spec, argv=argv):
                run = Fake([info(), HEAD])
                code, out, err = self.main(
                    ["worktree", spec, "--branch", branch, *argv, "--dir", self.dir], run
                )
                wt = os.path.join(self.dir, path)
                self.assertEqual((code, json.loads(out)["worktree"]), (0, wt), err)
                self.assertTrue(run.ran("gh", "repo", "clone", f"github.com/{spec}", wt))

    def test_a_bad_name_is_refused_before_the_clone_is_read(self):
        clone = os.path.join(self.dir, "clone")
        os.makedirs(os.path.join(clone, ".git"))
        for cmd in ("worktree", "status"):
            for spec in ("o/n", clone):
                for name in ("", "a/b", "../x", "a b", "x;y", "~", "x" * 101):
                    with self.subTest(cmd=cmd, spec=spec, name=name):
                        run = Fake([])
                        code, _, err = self.main(
                            [cmd, spec, "--branch", "TASK-1-x", "--name", name, "--dir", self.dir],
                            run,
                        )
                        self.assertEqual((code, run.calls), (2, []))
                        self.assertIn("unsafe checkout name", err)
        self.assertEqual(os.listdir(self.dir), ["clone"])

    def test_anything_else_in_the_way_is_invalid(self):
        def other_repo():
            return [self.existing("https://github.com/x/n")]

        def a_worktree_for_a_remote_repo():
            os.makedirs(self.wt)
            with open(os.path.join(self.wt, ".git"), "w") as f:
                f.write("gitdir: /elsewhere/.git/worktrees/n\n")
            return []

        def a_symlink():
            os.makedirs(os.path.dirname(self.wt))
            os.symlink(self.dir, self.wt)
            return []

        for setup, want in (
            (other_repo, "not a checkout of github.com/o/n"),
            (a_worktree_for_a_remote_repo, "not a checkout of github.com/o/n"),
            (a_symlink, "exists and is not a checkout"),
        ):
            with self.subTest(setup.__name__):
                (code, _, err), run = self.worktree(*setup(), info())
                self.assertEqual(code, 2, err)
                self.assertIn(want, err)
                self.assertFalse(run.ran("gh", "repo", "clone"))
                shutil.rmtree(os.path.join(self.dir, "o"))

    def test_not_found_is_invalid(self):
        (code, _, err), run = self.worktree((["gh", "api"], fail("gh: Not Found (HTTP 404)")))
        self.assertEqual(code, 2)
        self.assertIn("HTTP 404", err)
        self.assertFalse(run.ran("gh", "repo", "clone"))

    def test_a_token_in_git_output_is_redacted(self):
        token = fail("fatal: could not read Password for 'https://TOK@github.com'")
        (code, _, err), _ = self.worktree(self.existing(), info(), (lambda a: "fetch" in a, token))
        self.assertEqual(code, 1)
        self.assertNotIn("TOK", err)
        self.assertIn("https://***@github.com", err)

    def test_network_failure_exits_1(self):
        (code, _, err), _ = self.worktree(
            info(), (["gh", "repo", "clone"], fail("connection reset"))
        )
        self.assertEqual(code, 1)
        self.assertIn("connection reset", err)

    def test_unreadable_repo_is_invalid(self):
        (code, _, err), run = self.worktree(info(), spec="not a repo")
        self.assertEqual(code, 2)
        self.assertIn("unreadable repo", err)
        self.assertEqual(run.calls, [])

    def test_a_local_path_whose_origin_names_no_repo_is_invalid(self):
        clone = os.path.join(self.dir, "clone")
        os.makedirs(os.path.join(clone, ".git"))
        (code, _, err), run = self.worktree(
            (lambda a: a[3:] == ["remote", "get-url", "origin"], ok("/srv/n.git\n")), spec=clone
        )
        self.assertEqual(code, 2)
        self.assertIn("no origin URL naming host/owner/name", err)
        self.assertFalse(run.ran("gh"))

    def test_an_unsafe_branch_is_invalid_before_the_repo_is_read(self):
        for branch in ("-x", "a..b", "a b", "x/"):
            with self.subTest(branch=branch):
                (code, _, err), run = self.worktree(info(), branch=branch, spec="not a repo")
                self.assertEqual((code, run.calls), (2, []), err)
                self.assertIn("unsafe branch name", err)

    def refused(self, argv):
        """argparse's stderr for `argv`, which it must refuse."""
        err = io.StringIO()
        with self.assertRaises(SystemExit), unittest.mock.patch("sys.stderr", err):
            repo.main(argv, config=self.none, local=self.none, run=Fake([]))
        return err.getvalue()

    def test_an_option_given_twice_is_refused(self):
        rows = [
            (cmd, argv, opt)
            for cmd in ("worktree", "status")
            for argv, opt in (
                (
                    [
                        "--dir",
                        self.dir,
                        "--branch",
                        "b",
                        "o/n",
                        "--dir",
                        os.path.expanduser("~/.claude"),
                    ],
                    "--dir",
                ),
                (["--dir", self.dir, "--branch", "b", "o/n", "--branch", "c"], "--branch"),
                (
                    ["--dir", self.dir, "--branch", "b", "--name", "x", "o/n", "--name", "y"],
                    "--name",
                ),
                (
                    ["--dir", self.dir, "--branch", "b", "--base", "a", "--base", "b", "o/n"],
                    "--base",
                ),
            )
        ]
        for cmd, argv, opt in rows:
            with self.subTest(cmd=cmd, opt=opt):
                self.assertIn(f"{opt} given twice", self.refused([cmd, *argv]))

    def test_a_bad_command_line_is_refused(self):
        """--branch is required; only worktree and status exist (prepare and checkout are gone)."""
        for argv, want in (
            (["worktree", "--dir", self.dir, "o/n"], "--branch"),
            (["status", "--dir", self.dir, "o/n"], "--branch"),
            (["prepare", "--dir", self.dir, "--branch", "b", "o/n"], "prepare"),
            (["checkout", "--dir", self.dir, "--branch", "b", "o/n"], "checkout"),
        ):
            with self.subTest(argv=argv):
                self.assertIn(want, self.refused(argv))
