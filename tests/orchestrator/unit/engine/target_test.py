"""Repository descriptions, target validation and branch selection."""

import json
import os
import shutil
import tempfile
from types import SimpleNamespace
from unittest import mock

from ensemble.orchestrator.engine import config, target
from ensemble.orchestrator.engine.flow import handoff
from ensemble.orchestrator.engine.flow.types import (
    Issue,
)
from ensemble.utils import SHORT, check_ref
from ensemble.utils import Invalid as RefInvalid
from tests.delegate.fakes.delegate import FakeDelegate
from tests.delegate.workdir import checkout_path
from tests.orchestrator.case import Case
from tests.orchestrator.config_fixtures import HARNESS_DELEGATE
from tests.orchestrator.target_fixtures import LOCAL
from tests.orchestrator.unit.target_fixtures import FakeRun, api, ok

PROJ = "121166b1-191a-4461-bec4-42f1c2dc0ddd"

NO_LINE = target.Invalid("no Repo: line in the description or its ## Instructions")


def roles():
    """Each role's tasks: the committed core config's index (no local file)."""
    index = HARNESS_DELEGATE.roles()
    return {r: SimpleNamespace(tasks=tuple(index[r])) for r in config.TASKS}


def issue(description, title="ENG: Session Registry", ident="TASK-26", project=None):
    return Issue(
        id="uuid-26",
        identifier=ident,
        url=f"https://linear.app/t/issue/{ident}",
        title=title,
        description=description,
        created_at="2026-09-01T00:00:00.000Z",
        home=project,
        notes=(),
        links=(),
        linked=(),
        handoff=handoff.parse_handoff(description),
    )


class RepositoryDescription(Case):
    def test_enterprise_host_is_explicit_and_constrained(self):
        for value in (
            "acme/app",
            "https://ghe.example.com/acme/app.git",
            "git@ghe.example.com:acme/app.git",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    target.parse_repo("Repo: " + value, host="ghe.example.com"), ("acme", "app")
                )
        self.assertIsInstance(
            target.parse_repo("Repo: https://github.com/acme/app", host="ghe.example.com"),
            target.Invalid,
        )

    def test_enterprise_target_uses_host_for_api_and_branch_lookup(self):
        run = FakeRun([(("gh", "api"), api("acme/app")), (("git",), ok())])
        got = target.check(
            issue("Repo: https://ghe.example.com/acme/app"),
            {},
            run=run,
            delegate=FakeDelegate,
            workdir=self.home,
            roles=roles(),
            host="ghe.example.com",
        )
        self.assertEqual((got.owner, got.name, got.host), ("acme", "app", "ghe.example.com"))
        self.assertIn("--hostname", run.calls[0][0])
        self.assertIn("ghe.example.com", run.calls[0][0])
        self.assertIn("https://ghe.example.com/acme/app.git", run.calls[-1][0])

    def test_enterprise_clone_selection_does_not_reuse_public_clone(self):
        t = target.Target("acme", "app", host="ghe.example.com")
        clones = {"github.com/acme/app": "/public", "ghe.example.com/acme/app": "/enterprise"}
        got = target.with_clone(
            t, "TASK-26", delegate=FakeDelegate, workdir=self.home, clones=clones
        )
        self.assertEqual(got.clone, "/enterprise")

    def test_accepted(self):
        for line in (
            "Repo: acme/ensemble",
            "repo: https://github.com/acme/ensemble",
            "REPO: https://github.com/acme/ensemble.git/",
            "Repo: git@github.com:acme/ensemble.git",
            "repo: [https://github.com/acme/ensemble](<https://github.com/acme/ensemble>)",
            "Repo: [ensemble](https://github.com/acme/ensemble)",
            "  repo:   acme/ensemble  ",
            "`Repo: acme/ensemble`",
            "* Repo: acme/ensemble",
            "- Repo: acme/ensemble",
            "> Repo: acme/ensemble",
            "**Repo:** acme/ensemble",
            "**Repo: acme/ensemble**",
            "Repo: `acme/ensemble`",
        ):
            with self.subTest(line=line):
                self.assertEqual(target.parse_repo(f"x\n{line}\ny"), ("acme", "ensemble"))
        for d, want in (
            ("a\r\nRepo: acme/ensemble\r\nb", ("acme", "ensemble")),
            ("Repo: acme/a\nrepo: ACME/A", ("acme", "a")),
            ("Repo: acme/.github", ("acme", ".github")),
            ("Repo: https://github.com/acme/.github.git", ("acme", ".github")),
            ("Repo: acme/..x", ("acme", "..x")),
            ("Repo: acme/a.git", ("acme", "a.git")),
            ("Repo: git@github.com:acme/a", ("acme", "a")),
            ("Repo: https://github.com/acme/a/", ("acme", "a")),
        ):
            with self.subTest(d=d):
                self.assertEqual(target.parse_repo(d), want)

    def test_comments_section_ignored(self):
        d = "Repo: acme/a\n\n## Comments\n* x:\n  > Repo: acme/b"
        self.assertEqual(target.parse_repo(d), ("acme", "a"))
        self.assertIsInstance(target.parse_repo("## Comments\nRepo: acme/a"), target.Invalid)

    def test_rejected(self):
        for d in (
            *(
                f"Repo: {v}"
                for v in (
                    "",
                    "acme",
                    "-acme/a",
                    "acme/-a",
                    "acme/..",
                    "acme/.",
                    "a b/c",
                    "acme/a;rm",
                    "https://gitlab.com/acme/a",
                    "http://github.com/acme/a",
                    "ssh://git@github.com/acme/a",
                    "git@github.com:acme/a/",
                    "github.com/acme/a",
                    "acme/a/",
                    "https://github.com/acme/a/tree/main",
                    "https://github.com/acme/a?x=1",
                )
            ),
            "Repo: acme/a\nRepo: acme/b",
        ):
            with self.subTest(d=d):
                self.assertIsInstance(target.parse_repo(d), target.Invalid)

    def test_no_line_and_messages(self):
        self.assertEqual(target.NO_LINE, NO_LINE)
        self.assertEqual(target.parse_repo(None), NO_LINE)
        self.assertEqual(
            target.parse_repo("Repo: nope"), target.Invalid("unreadable Repo line: 'nope'")
        )
        self.assertEqual(
            target.parse_repo("Repo: acme/a\nRepo: acme/b"),
            target.Invalid("several different Repo: values"),
        )


class RepositorySlug(Case):
    def test_rules(self):
        self.assertEqual(target.slug("ENG: Session Registry (MVP)!"), "session-registry-mvp")
        self.assertEqual(target.slug("Engineering 骨架"), "engineering")
        self.assertEqual(target.slug("ENG: 中文标题"), "build")
        self.assertEqual(len(target.slug("ENG: " + "a" * 30 + " " + "b" * 30)), 40)
        self.assertFalse(target.slug("ENG: " + "a" * 39 + " bbb").endswith("-"))


class PullRequestTitles(Case):
    def test_keeps_unicode_letters_and_digits_and_removes_what_single_quotes_cannot_hold(self):
        for title, want in (
            ("ENG: Engineering 骨架", "TASK-38: Engineering 骨架"),
            ("ENG: 中文标题 2", "TASK-38: 中文标题 2"),
            (
                "ENG: a $(rm -rf ~) `x` 'q' \"d\"\nnext\ttab\x07 b_c.d,e:(f)/g-h‮",
                "TASK-38: a (rm -rf ) x q d next tab b_c.d,e:(f)/g-h",
            ),
        ):
            with self.subTest(title=title):
                self.assertEqual(target.pr_title("TASK-38", title), want)


class ResearchRepository(Case):
    REPOS = {PROJ: "acme/ensemble"}

    def test_mapped_repo_only_without_a_repo_line(self):
        for desc, project, repos, want in (
            ("no repo here", PROJ, self.REPOS, target.Target("acme", "ensemble")),
            ("", PROJ, self.REPOS, target.Target("acme", "ensemble")),
            ("Repo: acme/other", PROJ, self.REPOS, None),
            ("Repo: nope", PROJ, self.REPOS, None),
            ("Repo: acme/a\nRepo: acme/b", PROJ, self.REPOS, None),
            ("no repo here", "other", self.REPOS, None),
            ("no repo here", None, self.REPOS, None),
            ("no repo here", PROJ, {}, None),
        ):
            with self.subTest(desc=desc, project=project, repos=repos):
                self.assertEqual(target.research_repo(issue(desc, project=project), repos), want)


class TargetValidation(Case):
    REPOS = {PROJ: "acme/ensemble"}

    MAPPED = {"desc": "no repo here", "repos": REPOS, "project": PROJ}

    ISSUES = (
        ("Repo line", {}, ""),
        ("Repo line, mapped project", {"repos": REPOS, "project": PROJ}, ""),
        ("mapped project", MAPPED, "project mapping "),
    )

    def setUp(self):
        self.work = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.workdir = os.path.join(self.work, "TASK-26")
        self.clone = checkout_path(self.workdir, "acme", "ensemble", "TASK-26")
        self.src = os.path.dirname(os.path.dirname(self.clone))
        self.ls_remote = [
            "git",
            "-c",
            "credential.helper=",
            "-c",
            "credential.helper=!gh auth git-credential",
            "ls-remote",
            "--heads",
            "https://github.com/acme/ensemble.git",
            "TASK-26-*",
        ]

    def git_dir(self):
        os.makedirs(os.path.join(self.clone, ".git"))

    def git_file(self):
        os.makedirs(self.clone)
        with open(os.path.join(self.clone, ".git"), "w") as f:
            f.write("gitdir: /elsewhere/.git/worktrees/ensemble\n")

    def branch_list(self, at=None):
        at = at or self.clone
        return (
            *LOCAL,
            at,
            f"--git-dir={at}/.git",
            "branch",
            "--list",
            "TASK-26-*",
            "--format=%(refname:short)",
        )

    def check(
        self,
        desc="Repo: acme/ensemble",
        repos=None,
        project=None,
        title="ENG: Session Registry",
        **over,
    ):
        t = {"api": api(), "local": ok(""), "remote": ok("")}
        t.update(over)
        self.run_ = FakeRun(
            [
                (("gh", "api"), t["api"]),
                (LOCAL, t["local"]),
                (("git", "-c", "credential.helper="), t["remote"]),
            ]
        )
        return target.check(
            issue(desc, title=title, project=project),
            repos or {},
            run=self.run_,
            delegate=FakeDelegate,
            workdir=self.workdir,
            roles=roles(),
        )

    def argvs(self):
        return [c[0] for c in self.run_.calls]

    def test_the_checkout_is_the_repos_among_those_its_delegate_reports(self):
        class Reporting(FakeDelegate):
            found = None

            @classmethod
            def checkouts(cls, workdir):
                cls.asked = workdir
                return cls.found

        other = os.path.join(self.src, "acme", "other-TASK-26")
        for found, want in (
            (None, None),
            ((), None),
            ((other, os.path.join(self.src, "acme", "ensemble-TASK-27")), None),
            ((other, self.clone), self.clone),
        ):
            with self.subTest(found=found):
                Reporting.found = found
                self.assertEqual(
                    target.checkout(
                        "TASK-26", "acme", "ensemble", delegate=Reporting, workdir=self.workdir
                    ),
                    want,
                )
                self.assertEqual(Reporting.asked, self.workdir)

    def test_ok_without_a_local_clone_asks_the_remote(self):
        self.assertEqual(
            self.check(), target.Target("acme", "ensemble", "TASK-26-session-registry")
        )
        self.assertEqual(
            self.run_.calls,
            [
                (("gh", "api", "repos/acme/ensemble"), SHORT),
                (tuple(self.ls_remote), SHORT),
            ],
        )

    def test_default_runner_uses_the_current_path_and_environment(self):
        seen = []

        def subprocess_run(argv, **kwargs):
            seen.append((argv, kwargs))
            return api() if argv[0] == "gh" else ok("")

        with (
            mock.patch.dict(os.environ, {"ENSEMBLE_PATH": "/target-tools", "TARGET_ENV": "kept"}),
            mock.patch("subprocess.run", side_effect=subprocess_run),
        ):
            expected_path = config.path()
            self.assertEqual(
                target.check(
                    issue("Repo: acme/ensemble"),
                    {},
                    delegate=FakeDelegate,
                    workdir=self.workdir,
                    roles=roles(),
                ),
                target.Target("acme", "ensemble", "TASK-26-session-registry"),
            )
        self.assertEqual(len(seen), 2)
        for _, kwargs in seen:
            self.assertEqual(kwargs["env"]["PATH"], expected_path)
            self.assertEqual(kwargs["env"]["TARGET_ENV"], "kept")
            self.assertEqual(kwargs["timeout"], SHORT)

    def test_where_the_branches_are_read(self):
        elsewhere = os.path.join(self.work, "elsewhere")
        os.makedirs(os.path.join(elsewhere, ".git"))

        def link(dest):
            os.makedirs(self.clone)
            os.symlink(dest, os.path.join(self.clone, ".git"))

        def at(*parts):
            return lambda: os.makedirs(os.path.join(self.src, *parts))

        local, remote = [(self.branch_list(), SHORT)], [(tuple(self.ls_remote), SHORT)]
        cases = [
            ("clone with branches", self.git_dir, "TASK-26-local", local),
            ("clone without branches", self.git_dir, "", local + remote),
            ("worktree", self.git_file, "TASK-26-planted", remote),
            (
                "symlinked .git",
                lambda: link(os.path.join(elsewhere, ".git")),
                "TASK-26-local",
                local,
            ),
            (
                "dangling .git symlink",
                lambda: link(os.path.join(elsewhere, "gone")),
                "TASK-26-planted",
                remote,
            ),
            ("legacy ensemble", at("ensemble", ".git"), "TASK-26-old", remote),
            ("legacy acme/ensemble", at("acme", "ensemble", ".git"), "TASK-26-old", remote),
            ("no clone dir", at("acme"), "TASK-26-old", remote),
        ]
        for label, setup, out, calls in cases:
            with self.subTest(label):
                shutil.rmtree(os.path.join(self.work, "TASK-26"), ignore_errors=True)
                setup()
                r = self.check(local=ok(out + "\n"), remote=ok("abc\trefs/heads/TASK-26-remote\n"))
                self.assertEqual(
                    r.branch, "TASK-26-local" if out == "TASK-26-local" else "TASK-26-remote"
                )
                self.assertEqual(self.run_.calls[1:], calls)

    def test_remote_names_are_taken_after_refs_heads(self):
        r = self.check(remote=ok("abc\trefs/heads/TASK-26-a\nabc\trefs/tags/x\n"))
        self.assertEqual(r.branch, "TASK-26-a")
        self.assertEqual(
            self.check(remote=ok("abc\trefs/heads/TASK-26-a\nabc\trefs/heads/TASK-26-b\n")),
            target.Invalid("several TASK-26-* branches: TASK-26-a, TASK-26-b"),
        )

    def test_read_only_task_branches_are_not_the_build_branch(self):
        read_only = "TASK-26-deep-research\nTASK-26-light-research\nTASK-26-product-design\n"
        self.git_dir()
        for out, branch, remote in (
            ("TASK-26-session-registry\n" + read_only, "TASK-26-session-registry", False),
            (read_only, "TASK-26-session-registry", True),
            (
                "TASK-26-light-research-2\nTASK-26-light-research\n",
                "TASK-26-light-research-2",
                False,
            ),
            ("TASK-26-build\nTASK-26-light-research\n", "TASK-26-build", False),
        ):
            with self.subTest(out=out):
                self.assertEqual(
                    self.check(local=ok(out)), target.Target("acme", "ensemble", branch)
                )
                self.assertEqual(
                    self.argvs()[1:], [self.branch_list()] + [tuple(self.ls_remote)] * remote
                )

    def test_slug_default_and_branch_validation(self):
        self.git_dir()
        self.assertEqual(self.check(title="Fix: it's 骨架").branch, "TASK-26-fix-it-s")
        names = [f"TASK-26-{c}" + "a" * 40 for c in "abcdef"]
        for over, want in (
            (
                {"local": ok("TASK-26-a\nTASK-26-b\n")},
                target.Invalid("several TASK-26-* branches: TASK-26-a, TASK-26-b"),
            ),
            (
                {"local": ok("TASK-26-Bad$(x)\n")},
                target.Invalid(
                    "existing branch name 'TASK-26-Bad$(x)' is not TASK-26-<lowercase slug>"
                ),
            ),
            (
                {"local": ok("TASK-26-" + "a" * 41 + "\n")},
                target.Invalid(
                    f"existing branch name {'TASK-26-' + 'a' * 41!r} is not TASK-26-<lowercase slug>"
                ),
            ),
            (
                {"local": ok("TASK-26-" + "a" * 40 + "\n")},
                target.Target("acme", "ensemble", "TASK-26-" + "a" * 40),
            ),
            (
                {"local": ok("TASK-27-x\n")},
                target.Invalid("existing branch name 'TASK-27-x' is not TASK-26-<lowercase slug>"),
            ),
            (
                {"local": ok("".join(n + "\n" for n in names))},
                target.Invalid("several TASK-26-* branches: " + ", ".join(names)[:200]),
            ),
        ):
            with self.subTest(want=want):
                self.assertEqual(self.check(**over), want)

    def test_branch_lookup_failures_are_transient(self):
        self.git_dir()
        want = target.Transient("git branch --list: fatal")
        for issue_label, extra, _ in self.ISSUES:
            with self.subTest(issue_label):
                self.assertEqual(self.check(local=ok(code=128, stderr="fatal"), **extra), want)
        shutil.rmtree(self.clone)
        for issue_label, extra, _ in self.ISSUES:
            with self.subTest(issue_label):
                self.assertEqual(
                    self.check(remote=ok(code=128, stderr="no remote"), **extra),
                    target.Transient("git ls-remote: no remote"),
                )

    def test_repo_line_or_project_mapping(self):
        found = target.Target("acme", "ensemble", "TASK-26-session-registry")
        gh = ("gh", "api", "repos/acme/ensemble")
        for desc, repos, project, want in (
            ("no repo here", self.REPOS, PROJ, found),
            ("Repo: acme/ensemble", {PROJ: "acme/other"}, PROJ, found),
            ("Repo: nope", self.REPOS, PROJ, target.Invalid("unreadable Repo line: 'nope'")),
            (
                "Repo: acme/a\nRepo: acme/b",
                self.REPOS,
                PROJ,
                target.Invalid("several different Repo: values"),
            ),
            ("no repo here", self.REPOS, "other", NO_LINE),
            ("no repo here", self.REPOS, None, NO_LINE),
            ("no repo here", {}, PROJ, NO_LINE),
            (
                "no repo here",
                {PROJ: "nope"},
                PROJ,
                target.Invalid("project mapping 'nope': not <owner>/<name>", mapped=True),
            ),
            (
                "no repo here",
                {PROJ: 42},
                PROJ,
                target.Invalid("project mapping '42': not <owner>/<name>", mapped=True),
            ),
        ):
            with self.subTest(desc=desc, repos=repos, project=project):
                self.assertEqual(self.check(desc, repos=repos, project=project), want)
                self.assertEqual(self.argvs()[:1], [gh] if want == found else [])
                self.assertNotIn(("gh", "api", "repos/acme/other"), self.argvs())

    def test_access_and_response_failures(self):
        def default(name):
            return api(default=name, push=True)

        unsafe = target.Invalid("acme/ensemble: unsafe default branch name")
        for label, res, want, tagged in (
            (
                "HTTP 404",
                ok(code=1, stderr="gh: Not Found (HTTP 404)"),
                target.Invalid("acme/ensemble: not found or no access (HTTP 404)"),
                True,
            ),
            (
                "HTTP 403",
                ok(code=1, stderr="gh: Forbidden (HTTP 403)"),
                target.Invalid("acme/ensemble: not found or no access (HTTP 403)"),
                True,
            ),
            (
                "HTTP 502",
                ok(code=1, stderr="gh: Bad Gateway (HTTP 502)"),
                target.Transient("gh api repos/acme/ensemble: gh: Bad Gateway (HTTP 502)"),
                False,
            ),
            (
                "no HTTP code",
                ok(code=1, stderr="error connecting to api.github.com"),
                target.Transient("gh api repos/acme/ensemble: error connecting to api.github.com"),
                False,
            ),
            (
                "no push",
                api(push=False),
                target.Invalid("acme/ensemble: no push permission"),
                True,
            ),
            (
                "push null",
                ok(
                    json.dumps(
                        {
                            "full_name": "acme/ensemble",
                            "default_branch": "main",
                            "permissions": {"push": None},
                        }
                    )
                ),
                target.Invalid("acme/ensemble: no push permission"),
                True,
            ),
            ("default -x", default("-x"), unsafe, True),
            ("default a..b", default("a..b"), unsafe, True),
            ("default ma$(x)", default("ma$(x)"), unsafe, True),
            ("default null", default(None), unsafe, True),
        ):
            for issue_label, extra, prefix in self.ISSUES:
                with self.subTest(label, issue=issue_label):
                    self.assertEqual(
                        self.check(api=res, **extra),
                        target.Invalid(prefix + want.reason, mapped=bool(prefix))
                        if tagged
                        else want,
                    )
                    self.assertEqual(self.argvs(), [("gh", "api", "repos/acme/ensemble")])

    def test_unusable_response_is_transient(self):
        good = {
            "full_name": "acme/ensemble",
            "default_branch": "main",
            "permissions": {"push": True},
        }

        def without(key):
            return json.dumps({k: v for k, v in good.items() if k != key})

        for label, out in (
            ("not JSON", "<html>"),
            ("empty", ""),
            ("a list", "[]"),
            ("null", "null"),
            ("no permissions", without("permissions")),
            ("no default_branch", without("default_branch")),
            ("no full_name", without("full_name")),
            ("permissions null", json.dumps({**good, "permissions": None})),
            ("full_name not owner/name", json.dumps({**good, "full_name": "nope"})),
            ("full_name not a string", json.dumps({**good, "full_name": 7})),
        ):
            with self.subTest(label):
                r = self.check(api=ok(out))
                self.assertIsInstance(r, target.Transient)
                self.assertTrue(r.reason.startswith("gh api repos/acme/ensemble: "), r)
                self.assertEqual(self.argvs(), [("gh", "api", "repos/acme/ensemble")])

    def test_o_and_n_come_from_full_name(self):
        renamed = os.path.join(self.src, "Acme", "Ensemble-2-TASK-26")
        os.makedirs(os.path.join(renamed, ".git"))
        for desc, asked in (
            ("Repo: ACME/ensemble", "ACME/ensemble"),
            ("Repo: https://github.com/acme/Ensemble.git", "acme/Ensemble"),
        ):
            with self.subTest(desc):
                r = self.check(desc, api=api("Acme/Ensemble-2"), local=ok("TASK-26-x\n"))
                self.assertEqual(r, target.Target("Acme", "Ensemble-2", "TASK-26-x"))
                self.assertEqual(self.run_.calls[0][0], ("gh", "api", f"repos/{asked}"))
                self.assertEqual(self.argvs()[1], self.branch_list(renamed))
        r = self.check(api=api("Acme/Ensemble-2"), remote=ok("abc\trefs/heads/TASK-26-y\n"))
        self.assertEqual(r, target.Target("Acme", "Ensemble-2", "TASK-26-y"))
        self.assertEqual(
            self.argvs()[-1][-2:], ("https://github.com/Acme/Ensemble-2.git", "TASK-26-*")
        )


class StageKeys(Case):
    """target.check with a stage's ref and mapped_only keys."""

    REPOS = {PROJ: "acme/ensemble"}
    BRANCHES = (
        "git",
        "-c",
        "credential.helper=",
        "-c",
        "credential.helper=!gh auth git-credential",
        "ls-remote",
        "--heads",
    )

    def check(self, desc, *, project: str | None = PROJ, repos=REPOS, api_=None, **keys):
        self.run_ = FakeRun(
            [
                (("gh", "api"), api_ or api()),
                (self.BRANCHES, ok("")),
            ]
        )
        return target.check(
            issue(desc, project=project),
            repos,
            run=self.run_,
            delegate=FakeDelegate,
            workdir=os.path.join(self.home, "TASK-26"),
            roles=roles(),
            **keys,
        )

    def argvs(self):
        return [c[0] for c in self.run_.calls]

    def test_mapped_only_rejects_an_unmapped_repo_line_before_any_command(self):
        for line, named in (
            ("Repo: acme/other", "acme/other"),
            ("Repo: https://github.com/other/ensemble", "other/ensemble"),
        ):
            with self.subTest(line=line):
                got = self.check(line, api_=api(named), mapped_only=True)
                self.assertEqual(
                    got,
                    target.Invalid(
                        f"{named}: this stage takes only its project's mapped repo acme/ensemble"
                    ),
                )
                self.assertEqual(self.argvs(), [])

    def test_mapped_only_rejects_a_project_without_mapping(self):
        for desc, project, repos in (
            ("Repo: acme/ensemble", "other", self.REPOS),
            ("no repo here", "other", self.REPOS),
            ("Repo: acme/ensemble", None, self.REPOS),
            ("Repo: acme/ensemble", PROJ, {}),
        ):
            with self.subTest(desc=desc, project=project, repos=repos):
                got = self.check(desc, project=project, repos=repos, mapped_only=True)
                self.assertEqual(
                    got,
                    target.Invalid(
                        "this stage takes only its project's mapped repo, and project "
                        f"{project!r} has none",
                        mapped=True,
                    ),
                )
                self.assertEqual(self.argvs(), [])

    def test_mapped_only_accepts_the_mapping(self):
        for desc, asked in (
            ("no repo here", "acme/ensemble"),
            ("Repo: acme/ensemble", "acme/ensemble"),
            ("Repo: ACME/Ensemble", "ACME/Ensemble"),
            ("Repo: https://github.com/acme/ensemble", "acme/ensemble"),
        ):
            with self.subTest(desc=desc):
                got = self.check(desc, mapped_only=True)
                self.assertEqual(got, target.Target("acme", "ensemble", "TASK-26-session-registry"))
                self.assertEqual(self.argvs()[0], ("gh", "api", f"repos/{asked}"))

    def test_keys_unset_accept_a_pushable_repo_line(self):
        got = self.check("Repo: acme/other", api_=api("acme/other"))
        self.assertEqual(got, target.Target("acme", "other", "TASK-26-session-registry"))
        self.assertEqual(self.argvs()[0], ("gh", "api", "repos/acme/other"))

    def ref_check(self, desc, refs=None, **keys):
        """check with a Ref line's ls-remote answering refs (stdout or a result)."""
        self.run_ = FakeRun(
            [
                (("gh", "api"), api()),
                (self.REFS[:7], ok(refs) if isinstance(refs, str) else refs or ok()),
                (self.BRANCHES, ok("")),
            ]
        )
        return target.check(
            issue(desc, project=PROJ),
            self.REPOS,
            run=self.run_,
            delegate=FakeDelegate,
            workdir=os.path.join(self.home, "TASK-26"),
            roles=roles(),
            **{"ref": True, "mapped_only": True, **keys},
        )

    REFS = (
        "git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "credential.helper=",
        "-c",
        "credential.helper=!gh auth git-credential",
        "ls-remote",
        "https://github.com/acme/ensemble.git",
    )

    def test_bad_ref_runs_no_command(self):
        for desc, value in (
            ("Ref: refs/pull/1/head", "refs/pull/1/head"),
            ("Ref: --upload-pack=touch /tmp/x", "--upload-pack=touch /tmp/x"),
            ("Ref: -x", "-x"),
            ("Ref: a b", "a b"),
            ("Ref:", ""),
            ("Ref:   ", ""),
            ("- **Ref:** `x.lock`", "x.lock"),
            ("Ref: a\x1fb", "a\x1fb"),
            ("Ref: a\x0bb", "a\x0bb"),
            ("Ref: a\x85b", "a\x85b"),
            ("Ref: HEAD", "HEAD"),
            ("Ref: " + "ab12" * 10, "ab12" * 10),
        ):
            with self.subTest(desc=desc):
                with self.assertRaises(RefInvalid) as caught:
                    check_ref(value)
                got = self.ref_check(f"Build it.\n{desc}\nMore.")
                self.assertEqual(got, target.Invalid(f"Ref: {caught.exception}", line="Ref"))
                self.assertEqual(self.argvs(), [])
                self.assertFalse(os.path.exists("/tmp/x"))

    def test_two_ref_lines_are_invalid(self):
        for desc in ("Ref: main\nRef: main", "Ref: main\n* ref: dev", "Ref: -x\nRef: main"):
            with self.subTest(desc=desc):
                self.assertEqual(
                    self.ref_check(desc), target.Invalid("several Ref: lines", line="Ref")
                )
                self.assertEqual(self.argvs(), [])

    def test_unlisted_ref_is_invalid(self):
        for refs in (
            "",
            "abc\trefs/remotes/origin/refs/heads/feature/x\n",
            "abc\trefs/tags/feature/x^{}\n",
            "abc\trefs/heads/feature/x-2\n",
        ):
            with self.subTest(refs=refs):
                self.assertEqual(
                    self.ref_check("Ref: feature/x", refs),
                    target.Invalid(
                        "Ref: 'feature/x': no such branch or tag in acme/ensemble", line="Ref"
                    ),
                )
                self.assertEqual(
                    self.argvs()[-1],
                    (*self.REFS, "refs/heads/feature/x", "refs/tags/feature/x"),
                )

    def test_listed_tag_and_branch_pass(self):
        for desc, refs, want in (
            ("Ref: v1.2.0", "a\trefs/tags/v1.2.0\nb\trefs/tags/v1.2.0^{}\n", "v1.2.0"),
            ("- **Ref:** `v1.2.0`", "a\trefs/tags/v1.2.0\n", "v1.2.0"),
            ("ref:\tmain \r\nReference: TASK-1", "a\trefs/heads/main\n", "main"),
            ("Ref: main\n\n## Comments\nRef: dev", "a\trefs/heads/main\n", "main"),
        ):
            with self.subTest(desc=desc):
                self.assertEqual(
                    self.ref_check(desc, refs),
                    target.Target("acme", "ensemble", "TASK-26-session-registry", ref=want),
                )
                self.assertEqual(
                    self.argvs()[-1], (*self.REFS, f"refs/heads/{want}", f"refs/tags/{want}")
                )

    def test_ref_ls_remote_failure_is_transient(self):
        got = self.ref_check("Ref: main", ok(code=128, stderr="fatal: no access"))
        self.assertTrue(
            isinstance(got, target.Transient) and got.reason.startswith("git ls-remote: "), got
        )

    def test_no_ref_line_runs_no_ref_ls_remote(self):
        got = self.ref_check("Repo: acme/ensemble\nReference: x")
        self.assertEqual(got, target.Target("acme", "ensemble", "TASK-26-session-registry"))
        self.assertNotIn(self.REFS, [a[: len(self.REFS)] for a in self.argvs()])

    def test_keys_unset_ignore_ref_lines(self):
        got = self.check("Repo: acme/other\nRef: refs/pull/1/head", api_=api("acme/other"))
        self.assertEqual(got, target.Target("acme", "other", "TASK-26-session-registry"))
        self.assertEqual([a[:2] for a in self.argvs()], [("gh", "api"), ("git", "-c")])
        self.assertEqual(self.argvs()[1][:7], self.BRANCHES)
