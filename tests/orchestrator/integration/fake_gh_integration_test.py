import json
import os
import subprocess
import unittest

from tests.orchestrator import case
from tests.orchestrator.integration import fake_gh
from tests.orchestrator.target_fixtures import REMOTE
from tests.utils.sandbox import scratch

REPO = {"full_name": "acme/widgets", "default_branch": "main", "permissions": {"push": True}}
RAW = "Accept: application/vnd.github.raw+json"
PRD = "Product Design/2026-10-09-prd.md"
CONTENTS = {
    "acme/docs": {
        "main": {
            "Research/2026-10-09-TASK-7-x.md": "# R\nbody\n",
            "Research/2026-10-08-TASK-6-y.md": "older",
            "Research/notes/deep.md": "deep",
            PRD: "caf\u00e9\r\nlast line, no newline",
            "README.md": "readme",
        },
        "feature/x": {"Research/only-here.md": "on a branch"},
    },
    "acme/other": {"main": {"a.md": "other repo"}},
}


class Case(case.Case):
    def setUp(self):
        self.root = scratch(self)
        self.home, self.bin, self.repos = (
            os.path.join(self.root, d) for d in ("home", "bin", "repos")
        )
        for d in (self.home, self.bin, self.repos):
            os.mkdir(d)
        self.scenario = os.path.join(self.root, "scenario.json")
        self.env = case.child_env(
            {
                "HOME": self.home,
                "PATH": f"{self.bin}:/usr/bin:/bin",
                fake_gh.ENV: self.scenario,
            },
            case.REPO,
        )
        fake_gh.install(self.bin)
        fake_gh.git_shim(self.bin, self.home)

    def write(self, scenario):
        with open(self.scenario, "w") as f:
            json.dump(scenario, f)

    def run_cmd(self, *argv, env=None):
        return subprocess.run(
            argv, env=self.env if env is None else env, capture_output=True, text=True, timeout=30
        )

    def gh(self, *argv, **kw):
        return self.run_cmd("gh", *argv, **kw)


class Shim(Case):
    def test_install_writes_an_executable_gh(self):
        path = os.path.join(self.bin, "gh")
        self.assertTrue(os.access(path, os.X_OK))
        self.assertEqual(fake_gh.install(self.bin), path)

    def test_api_prints_the_scenarios_object(self):
        self.write({"repos": {"acme/widgets": REPO, "acme/other": {"full_name": "acme/other"}}})
        res = self.gh("api", "repos/acme/widgets")
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        self.assertEqual(json.loads(res.stdout), REPO)

    def test_an_unlisted_repo_is_a_404_on_stderr(self):
        self.write({"repos": {"acme/widgets": REPO}})
        for slug in ("acme/nope", "other/widgets", "Acme/widgets"):
            with self.subTest(slug):
                res = self.gh("api", f"repos/{slug}")
                self.assertEqual((res.returncode, res.stdout), (1, ""))
                self.assertIn("HTTP 404", res.stderr)

    def test_any_other_argv_is_a_usage_error(self):
        self.write({"repos": {"acme/widgets": REPO}})
        for argv in (
            (),
            ("api",),
            ("auth", "status"),
            ("api", "user"),
            ("api", "repos/acme"),
            ("api", "repos/acme/widgets/branches"),
            ("api", "repos/acme/widgets", "--jq", ".name"),
            ("api", "/repos/acme/widgets"),
            ("repo", "view", "acme/widgets"),
        ):
            with self.subTest(argv):
                res = self.gh(*argv)
                self.assertEqual((res.returncode, res.stdout), (1, ""))
                self.assertIn("fake_gh.py:", res.stderr)
                self.assertNotIn("HTTP", res.stderr)

    def test_a_malformed_scenario_is_an_error(self):
        for name, body in {
            "not json": "{",
            "not an object": "[]",
            "no repos": "{}",
            "unknown key": '{"repos": {}, "orgs": {}}',
            "repos not an object": '{"repos": []}',
            "repo not an object": '{"repos": {"acme/widgets": 1}}',
        }.items():
            with self.subTest(name):
                with open(self.scenario, "w") as f:
                    f.write(body)
                res = self.gh("api", "repos/acme/widgets")
                self.assertEqual((res.returncode, res.stdout), (1, ""))
                self.assertIn("fake_gh.py:", res.stderr)
                self.assertNotIn("HTTP", res.stderr)

    def test_a_missing_or_unreadable_scenario_is_an_error(self):
        for name, setup in {
            "missing": lambda: None,
            "a directory": lambda: os.mkdir(self.scenario),
        }.items():
            with self.subTest(name):
                setup()
                res = self.gh("api", "repos/acme/widgets")
                self.assertEqual((res.returncode, res.stdout), (1, ""))
                self.assertIn(f"fake_gh.py: {fake_gh.ENV}:", res.stderr)
                self.assertNotIn("HTTP", res.stderr)

    def test_an_unset_scenario_is_an_error(self):
        env = {k: v for k, v in self.env.items() if k != fake_gh.ENV}
        res = self.gh("api", "repos/acme/widgets", env=env)
        self.assertEqual((res.returncode, res.stdout), (1, ""))
        self.assertIn(f"fake_gh.py: {fake_gh.ENV} is unset", res.stderr)


class Contents(Case):
    def setUp(self):
        super().setUp()
        self.write({"repos": {"acme/widgets": REPO}, "contents": CONTENTS})

    def listing(self, *argv):
        res = self.gh("api", *argv)
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        return json.loads(res.stdout)

    def test_a_dir_is_a_sorted_list_of_its_direct_entries_and_a_file_its_entry(self):
        docs = "repos/acme/docs/contents/"
        for path, want in (
            (
                "Research",
                [
                    {
                        "name": "2026-10-08-TASK-6-y.md",
                        "path": "Research/2026-10-08-TASK-6-y.md",
                        "type": "file",
                    },
                    {
                        "name": "2026-10-09-TASK-7-x.md",
                        "path": "Research/2026-10-09-TASK-7-x.md",
                        "type": "file",
                    },
                    {"name": "notes", "path": "Research/notes", "type": "dir"},
                ],
            ),
            (
                "Research/notes",
                [{"name": "deep.md", "path": "Research/notes/deep.md", "type": "file"}],
            ),
            (
                "Product%20Design/2026-10-09-prd.md",
                {"name": "2026-10-09-prd.md", "path": PRD, "type": "file"},
            ),
            ("README.md", {"name": "README.md", "path": "README.md", "type": "file"}),
        ):
            with self.subTest(path=path):
                self.assertEqual(self.listing(f"{docs}{path}?ref=main"), want)

    def test_a_dir_with_raw_is_the_same_list(self):
        url = "repos/acme/docs/contents/Research?ref=main"
        self.assertEqual(self.listing("-H", RAW, url), self.listing(url))

    def test_raw_prints_the_file_verbatim(self):
        url = "repos/acme/docs/contents/Product%20Design/2026-10-09-prd.md?ref=main"
        res = subprocess.run(
            ["gh", "api", "-H", RAW, url], env=self.env, capture_output=True, timeout=30
        )
        text = CONTENTS["acme/docs"]["main"][PRD]
        self.assertEqual((res.returncode, res.stderr, res.stdout), (0, b"", text.encode()))

    def test_a_quoted_path_and_ref_are_decoded(self):
        self.assertEqual(
            self.listing("repos/acme/docs/contents/Product%20Design?ref=main"),
            [{"name": "2026-10-09-prd.md", "path": PRD, "type": "file"}],
        )
        res = self.gh(
            "api", "-H", RAW, "repos/acme/docs/contents/Research/only-here.md?ref=feature%2Fx"
        )
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "on a branch", ""))

    def test_repos_is_unchanged_beside_contents(self):
        self.assertEqual(self.gh("api", "repos/acme/widgets").stdout, json.dumps(REPO) + "\n")
        res = self.gh("api", "-H", RAW, "repos/acme/other/contents/a.md?ref=main")
        self.assertEqual((res.returncode, res.stdout), (0, "other repo"))

    def test_a_missing_path_branch_or_repo_is_a_404_on_stderr(self):
        docs = "repos/acme/docs/contents/"
        for url in (
            docs + "Nope?ref=main",
            docs + "Research/missing.md?ref=main",
            docs + "Resear?ref=main",
            docs + "Research/?ref=main",
            docs + "README.md?ref=other",
            docs + "Research/only-here.md?ref=main",
            docs + "README.md?ref=feature/x",
            "repos/acme/nope/contents/README.md?ref=main",
            "repos/Acme/docs/contents/README.md?ref=main",
            "repos/acme/widgets/contents/README.md?ref=main",
        ):
            for argv in (("api", url), ("api", "-H", RAW, url)):
                with self.subTest(argv):
                    res = self.gh(*argv)
                    self.assertEqual(
                        (res.returncode, res.stdout, res.stderr),
                        (1, "", "gh: Not Found (HTTP 404)\n"),
                    )

    def test_no_contents_key_is_a_404_for_every_path(self):
        self.write({"repos": {"acme/widgets": REPO}})
        res = self.gh("api", "repos/acme/docs/contents/README.md?ref=main")
        self.assertEqual(
            (res.returncode, res.stdout, res.stderr), (1, "", "gh: Not Found (HTTP 404)\n")
        )

    def test_any_other_argv_is_a_usage_error(self):
        docs = "repos/acme/docs/contents"
        url = f"{docs}/README.md?ref=main"
        for argv in (
            ("api", "-H", "Accept: application/json", url),
            ("api", "-H", RAW.lower(), url),
            ("api", "-H", RAW),
            ("api", "-H", url),
            ("api", "-H", RAW, "-H", RAW, url),
            ("api", url, "-H", RAW),
            ("api", "--header", RAW, url),
            ("api", "-H", RAW, "repos/acme/widgets"),
            ("api", f"{docs}/README.md"),
            ("api", f"{docs}/README.md?ref="),
            ("api", f"{docs}/README.md?branch=main"),
            ("api", f"{docs}/?ref=main"),
            ("api", f"{docs}?ref=main"),
            ("api", f"{docs}/README.md?ref=a?b"),
            ("api", url, "--jq", ".name"),
            ("api", url, url),
        ):
            with self.subTest(argv):
                res = self.gh(*argv)
                self.assertEqual((res.returncode, res.stdout), (1, ""))
                self.assertIn("fake_gh.py:", res.stderr)
                self.assertNotIn("HTTP", res.stderr)

    def test_a_bad_contents_shape_or_another_key_is_an_error(self):
        path = {"main": {"a.md": "x"}}
        for name, scenario in {
            "a list": {"contents": []},
            "a repo not an object": {"contents": {"acme/docs": 1}},
            "a branch not an object": {"contents": {"acme/docs": {"main": []}}},
            "a text not a string": {"contents": {"acme/docs": {"main": {"a.md": 5}}}},
            "a null text": {"contents": {"acme/docs": {"main": {"a.md": None}}}},
            "another key": {"contents": {"acme/docs": path}, "orgs": {}},
        }.items():
            self.write({"repos": {"acme/widgets": REPO}, **scenario})
            for argv in (
                ("api", "repos/acme/docs/contents/a.md?ref=main"),
                ("api", "repos/acme/widgets"),
            ):
                with self.subTest(name, argv=argv):
                    res = self.gh(*argv)
                    self.assertEqual((res.returncode, res.stdout), (1, ""))
                    self.assertIn("fake_gh.py:", res.stderr)
                    self.assertNotIn("HTTP", res.stderr)

    def test_repos_stays_required(self):
        self.write({"contents": CONTENTS})
        res = self.gh("api", "repos/acme/docs/contents/README.md?ref=main")
        self.assertEqual((res.returncode, res.stdout), (1, ""))
        self.assertIn("fake_gh.py:", res.stderr)


PR = {
    "number": 1,
    "url": "https://github.com/acme/widgets/pull/1",
    "state": "OPEN",
    "baseRefName": "main",
    "headRefName": "TASK-7-x",
}
COMMENT = {"created_at": "2026-10-14T00:00:00Z", "user": {"login": "me"}, "body": "Rename it."}
LIST = ("--state", "all", "--json", "number,url,headRefName", "--limit", "100")


class Pulls(Case):
    def setUp(self):
        super().setUp()
        self.write(
            {
                "repos": {"acme/widgets": REPO},
                "login": "me",
                "pulls": {"acme/widgets": [PR, {**PR, "number": 2, "headRefName": "other"}]},
                "threads": {"acme/widgets": {"issues/1/comments": [COMMENT]}},
            }
        )

    def ok(self, *argv):
        res = self.gh(*argv)
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        return json.loads(res.stdout)

    def test_user_is_the_scenarios_login_and_hostname_keeps_the_repo_api(self):
        for path, want in (("user", {"login": "me"}), ("repos/acme/widgets", REPO)):
            with self.subTest(path):
                self.assertEqual(self.ok("api", "--hostname", "github.com", path), want)

    def test_pr_list_gives_the_branch_prs_with_the_asked_fields(self):
        for repo in ("acme/widgets", "github.com/acme/widgets"):
            with self.subTest(repo):
                self.assertEqual(
                    self.ok("pr", "list", "--repo", repo, "--head", "TASK-7-x", *LIST),
                    [{"number": 1, "url": PR["url"], "headRefName": "TASK-7-x"}],
                )
        self.assertEqual(
            self.ok("pr", "list", "--repo", "acme/widgets", "--head", "TASK-8-y", *LIST), []
        )

    def test_a_thread_is_one_page_and_an_unlisted_one_empty(self):
        api = ("api", "--hostname", "github.com", "--paginate", "--slurp")
        self.assertEqual(self.ok(*api, "repos/acme/widgets/issues/1/comments"), [[COMMENT]])
        self.assertEqual(self.ok(*api, "repos/acme/widgets/pulls/1/reviews"), [[]])
        res = self.gh(*api, "repos/acme/nope/pulls/1/comments")
        self.assertEqual(
            (res.returncode, res.stdout, res.stderr), (1, "", "gh: Not Found (HTTP 404)\n")
        )

    def test_any_other_argv_is_a_usage_error(self):
        api = ("api", "--hostname", "github.com")
        for argv in (
            ("api", "--hostname", "example.com", "user"),
            (*api, "--paginate", "repos/acme/widgets/issues/1/comments"),
            (*api, "--paginate", "--slurp", "repos/acme/widgets/issues/x/comments"),
            ("pr", "list", "--repo", "acme/widgets", "--head", "TASK-7-x"),
            ("pr", "list", "--head", "TASK-7-x", "--repo", "acme/widgets", *LIST),
            ("pr", "list", "--repo", "acme/widgets", "--head", "TASK-7-x", *LIST[:-1], "10"),
            ("pr", "view", "1"),
            ("pr", "view", "https://github.com/acme/widgets/pull/1"),
            ("pr", "ready", "https://github.com/acme/widgets/pull/1", "--draft"),
        ):
            with self.subTest(argv):
                res = self.gh(*argv)
                self.assertEqual((res.returncode, res.stdout), (1, ""))
                self.assertIn("fake_gh.py:", res.stderr)

    def test_pr_view_prints_a_views_fields_and_pr_ready_sets_its_draft(self):
        url = "https://github.com/acme/widgets/pull/1"
        view = {"state": "OPEN", "isDraft": True, "headRefOid": "a" * 40, "statusCheckRollup": []}
        self.write({"repos": {"acme/widgets": REPO}, "views": {url: view}})
        self.assertEqual(
            self.ok("pr", "view", url, "--json", "state,isDraft,nope"),
            {"state": "OPEN", "isDraft": True},
        )
        for argv, draft in (((url,), False), ((url, "--undo"), True)):
            with self.subTest(argv):
                res = self.gh("pr", "ready", *argv)
                self.assertEqual((res.returncode, res.stderr), (0, ""))
                self.assertEqual(
                    self.ok("pr", "view", url, "--json", "isDraft"), {"isDraft": draft}
                )
        for argv in (("view", url + "0", "--json", "state"), ("ready", url + "0")):
            with self.subTest(argv):
                res = self.gh("pr", *argv)
                self.assertEqual((res.returncode, res.stdout), (1, ""))
                self.assertIn("no pull requests found", res.stderr)

    def test_no_login_and_bad_login_pulls_or_threads_are_errors(self):
        repo_api, user_api = ("repos/acme/widgets",), ("--hostname", "github.com", "user")
        for name, scenario, argv in (
            ("no login", {}, user_api),
            ("a login not a string", {"login": 1}, repo_api),
            ("pulls a list", {"pulls": []}, repo_api),
            ("a pull not an object", {"pulls": {"acme/widgets": [1]}}, repo_api),
            ("a pull without headRefName", {"pulls": {"acme/widgets": [{"number": 1}]}}, repo_api),
            ("threads of a list", {"threads": {"acme/widgets": []}}, repo_api),
            (
                "a comment not an object",
                {"threads": {"acme/widgets": {"issues/1/comments": [1]}}},
                repo_api,
            ),
            ("views a list", {"views": []}, repo_api),
            ("a view not an object", {"views": {"u": 1}}, repo_api),
        ):
            with self.subTest(name):
                self.write({"repos": {"acme/widgets": REPO}, **scenario})
                res = self.gh("api", *argv)
                self.assertEqual((res.returncode, res.stdout), (1, ""))
                self.assertIn("fake_gh.py:", res.stderr)


class Clone(Case):
    def test_repo_clone_gives_a_checkout_whose_ssh_origin_fetches_the_bare_repo(self):
        fake_gh.bare(self.repos, "acme", "widgets", branches=("main",))
        fake_gh.gitconfig(self.home, self.repos)
        self.write({"repos": {"acme/widgets": REPO}})
        wt = os.path.join(self.root, "wt")
        res = self.gh("repo", "clone", "github.com/acme/widgets", wt, "--", "--filter=blob:none")
        self.assertEqual(res.returncode, 0, res.stderr)
        url = self.run_cmd("git", "-C", wt, "remote", "get-url", "origin")
        self.assertEqual(url.stdout, "ssh://git@github.com/acme/widgets.git\n", url.stderr)
        fetch = self.run_cmd("git", "-C", wt, "fetch", "origin")
        self.assertEqual(fetch.returncode, 0, fetch.stderr)
        head = self.run_cmd("git", "-C", wt, "rev-parse", "--verify", "origin/main")
        self.assertEqual(head.returncode, 0, head.stderr)

    def test_a_bad_clone_argv_is_a_usage_error(self):
        self.write({"repos": {"acme/widgets": REPO}})
        for argv in (
            ("repo", "clone", "acme/widgets"),
            ("repo", "clone", "acme/widgets", "wt", "-x"),
            ("repo", "clone", "acme", "wt", "--"),
        ):
            with self.subTest(argv):
                res = self.gh(*argv)
                self.assertEqual((res.returncode, res.stdout), (1, ""))
                self.assertIn("fake_gh.py:", res.stderr)


class Bare(Case):
    def refs(self, path):
        res = self.run_cmd("git", "--git-dir", path, "for-each-ref", "--format=%(refname)")
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout.split()

    def test_bare_makes_the_repo_at_owner_name_dot_git(self):
        path = fake_gh.bare(self.repos, "acme", "widgets", ["TASK-7-x", "feature/y"])
        self.assertEqual(path, os.path.join(self.repos, "acme", "widgets.git"))
        res = self.run_cmd("git", "--git-dir", path, "rev-parse", "--is-bare-repository")
        self.assertEqual(res.stdout.strip(), "true")
        self.assertEqual(sorted(self.refs(path)), ["refs/heads/TASK-7-x", "refs/heads/feature/y"])

    def test_each_branch_is_a_commit(self):
        path = fake_gh.bare(self.repos, "acme", "widgets", ["TASK-7-x"])
        res = self.run_cmd("git", "--git-dir", path, "rev-list", "--count", "TASK-7-x")
        self.assertEqual(res.stdout.strip(), "1")

    def test_no_branches_is_an_empty_repo(self):
        path = fake_gh.bare(self.repos, "acme", "widgets")
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(self.refs(path), [])

    def test_gitconfig_maps_github_to_the_root(self):
        fake_gh.gitconfig(self.home, self.repos)
        with open(os.path.join(self.home, ".gitconfig")) as f:
            self.assertEqual(
                f.read(), f'[url "file://{self.repos}/"]\n\tinsteadOf = https://github.com/\n'
            )

    def test_ls_remote_through_the_gitconfig_lists_the_seeded_branch_and_only_it(self):
        fake_gh.bare(self.repos, "acme", "widgets", ["TASK-7-x", "TASK-8-y", "main"])
        fake_gh.bare(self.repos, "acme", "other", ["TASK-7-z"])
        fake_gh.gitconfig(self.home, self.repos)
        res = self.run_cmd(
            "git", "ls-remote", "--heads", "https://github.com/acme/widgets.git", "TASK-7-*"
        )
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        self.assertEqual(
            [line.split("\t")[1] for line in res.stdout.splitlines()], ["refs/heads/TASK-7-x"]
        )

    def test_ls_remote_of_a_repo_without_the_branch_is_empty(self):
        fake_gh.bare(self.repos, "acme", "widgets")
        fake_gh.gitconfig(self.home, self.repos)
        res = self.run_cmd(
            "git", "ls-remote", "--heads", "https://github.com/acme/widgets.git", "TASK-7-*"
        )
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))

    def test_targets_ls_remote_argv_works(self):
        fake_gh.bare(self.repos, "acme", "widgets", ["TASK-7-x"])
        fake_gh.gitconfig(self.home, self.repos)
        res = self.run_cmd(
            *REMOTE, "ls-remote", "--heads", "https://github.com/acme/widgets.git", "TASK-7-*"
        )
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        self.assertIn("refs/heads/TASK-7-x", res.stdout)


if __name__ == "__main__":
    unittest.main()
