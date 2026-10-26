"""src/ensemble/utils/git.py: common_dir over real git worktrees; git's lock retries."""

import os
import unittest
import unittest.mock
from types import SimpleNamespace

import ensemble.utils.git
from ensemble.utils import Invalid, check_ref
from ensemble.utils import git as repos
from tests.utils.case import Case


def ok(stdout=""):
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def fail(stderr):
    return SimpleNamespace(returncode=1, stdout="", stderr=stderr)


class Retry(Case):
    LOCKS = (
        "fatal: Unable to create '/w/.git/index.lock': File exists.",
        "error: cannot lock ref 'refs/remotes/origin/main': is at abc but expected def",
        "error: could not lock config file .git/config: File exists",
    )

    def git(self, *results, args=("fetch", "origin")):
        """(stdout or the RuntimeError, the argvs run, the mocked sleep)."""
        calls, answers = [], list(results)

        def run(argv, _timeout):
            calls.append(argv)
            return answers.pop(0)

        with unittest.mock.patch.object(ensemble.utils.git.time, "sleep") as sleep:
            try:
                return ensemble.utils.git.git("/w", *args, run=run), calls, sleep
            except RuntimeError as e:
                return e, calls, sleep

    def test_a_lock_failure_is_retried(self):
        for stderr in self.LOCKS:
            with self.subTest(stderr=stderr):
                out, calls, sleep = self.git(fail(stderr), ok("done"))
                self.assertEqual(
                    (out, calls), ("done", [["git", "-C", "/w", "fetch", "origin"]] * 2)
                )
                sleep.assert_called_once_with(0.5)

    def test_five_lock_failures_raise(self):
        err, calls, sleep = self.git(*[fail(self.LOCKS[0])] * 5)
        self.assertIsInstance(err, RuntimeError)
        self.assertIn("index.lock", str(err))
        self.assertEqual(len(calls), 5)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.5, 1, 2, 4])

    def test_the_command_text_shortens_long_arguments_and_stays_bounded(self):
        msg = str(
            self.git(
                fail("fatal: boom"),
                args=("--git-dir=" + "/private/var/folders/xy" * 10, "worktree", "prune"),
            )[0]
        )
        self.assertTrue(msg.startswith("git --git-dir=/private/var/folders/xy"), msg)
        self.assertTrue(msg.endswith("worktree prune: fatal: boom"), msg)
        self.assertLess(len(msg), 120)
        self.assertLess(
            len(str(self.git(fail("fatal: boom"), args=["/a/long/path/argument" * 3] * 50)[0])), 250
        )

    def test_other_failures_are_not_retried(self):
        err, calls, sleep = self.git(fail("fatal: not a git repository"), ok())
        self.assertIsInstance(err, RuntimeError)
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

    def test_pre_options_go_before_the_worktree_and_the_timeout_is_passed(self):
        seen = []
        ensemble.utils.git.git(
            "/w",
            "status",
            run=lambda argv, t: seen.append((argv, t)) or ok(),
            timeout=9,
            pre=("-c", "a=b"),
        )
        self.assertEqual(seen, [(["git", "-c", "a=b", "-C", "/w", "status"], 9)])


class Parse(Case):
    def test_local_origin_removes_clone_suffix_once(self):
        clone = os.path.join(self.home, "clone")
        os.makedirs(os.path.join(clone, ".git"))
        for host in ("github.com", "ghe.example.com"):
            with self.subTest(host=host):
                run = unittest.mock.Mock(return_value=ok(f"git@{host}:acme/app.git.git\n"))
                self.assertEqual(
                    repos.resolve(clone, run=run),
                    (repos.Repo(host, "acme", "app.git"), os.path.realpath(clone)),
                )

    def test_strict_github_repo_slugs(self):
        cases = {
            "acme/ensemble": ("acme", "ensemble"),
            "acme/.github": ("acme", ".github"),
            "Acme/Ensemble.git": ("Acme", "Ensemble.git"),
            "a/..x": ("a", "..x"),
            "-a/b": None,
            "a/-b": None,
            "a/..": None,
            "a/.": None,
            "a b/c": None,
            "acme": None,
            "https://github.com/acme/x": None,
            "github.com/acme/x": None,
            "acme/x/": None,
            " acme/x": None,
            "acme/x ": None,
            "": None,
            None: None,
            42: None,
        }
        for value, want in cases.items():
            with self.subTest(value=value):
                self.assertEqual(repos.repo_slug(value), want)

    def test_forms(self):
        for spec, want in (
            ("o/n", ("github.com", "o", "n")),
            ("ghe.example.com/o/n", ("ghe.example.com", "o", "n")),
            ("https://github.com/o/n.git", ("github.com", "o", "n")),
            ("https://GHE.io/o/n/", ("ghe.io", "o", "n")),
        ):
            self.assertEqual(repos.Repo.parse(spec), repos.Repo(*want), spec)

    def test_unreadable(self):
        for spec in ("n", "o/..", "https://o/n", "o/n; rm -rf /", "-o/n"):
            with self.assertRaises(repos.Invalid, msg=spec):
                repos.Repo.parse(spec)

    def test_url_forms(self):
        """url_slug keeps the case as written; origin lowercases it."""
        for url, want in (
            ("git@GitHub.com:O/N.git", "GitHub.com/O/N"),
            ("https://ghe.io/o/n", "ghe.io/o/n"),
            ("ssh://git@ghe.io:2222/o/n.git", "ghe.io/o/n"),
            ("ssh://ghe.io/o/n", "ghe.io/o/n"),
            ("https://user@github.com/o/n", "github.com/o/n"),
            ("https://user:tok@github.com/o/n.git", "github.com/o/n"),
            ("http://ghe.io/o/n/", "ghe.io/o/n"),
            ("https://ghe.io:8443/o/n", "ghe.io:8443/o/n"),
        ):
            with self.subTest(url=url):
                self.assertEqual(repos.url_slug(url), want)
                self.assertEqual(repos.origin(url), want.lower())
                repos.Repo.parse(want)
        for url in (
            "/tmp/origin.git",
            "ftp://ghe.io/o/n",
            "https://ghe.io/o",
            "ssh://git@ghe.io:x/o/n",
            "https://ghe.io/o/n/extra",
        ):
            with self.subTest(url=url):
                self.assertEqual((repos.url_slug(url), repos.origin(url)), (None, None))


class Checkout(Case):
    def test_checkout_is_the_path_rule(self):
        self.assertEqual(repos.checkout("/b", "o", "n", "TASK-1"), "/b/o/n-TASK-1")
        self.assertEqual(
            repos.checkout("b", "o", "n", "x.y_z-1"),
            os.path.join(os.getcwd(), "b", "o", "n-x.y_z-1"),
        )
        with self.assertRaises(repos.Invalid):
            repos.checkout("/b", "o", "n", "a/b")


class CheckRef(Case):
    def test_branch_and_tag_names_pass_unchanged(self):
        for ref in ("main", "v1.2.0", "feature/x", "release-1.0_rc", "abcdef", "a" * 255):
            with self.subTest(ref=ref):
                self.assertEqual(check_ref(ref), ref)

    def test_anything_else_is_invalid_and_named(self):
        for ref, why in (
            ("", "empty"),
            ("a" * 256, "longer than 255 characters"),
            ("--upload-pack=touch /tmp/x", "want letters, digits, ., _, / or -"),
            ("a b", "want letters, digits, ., _, / or -"),
            ("a\tb", "want letters, digits, ., _, / or -"),
            ("a\x1fb", "want letters, digits, ., _, / or -"),
            ("a\x0bb", "want letters, digits, ., _, / or -"),
            ("a\nb", "want letters, digits, ., _, / or -"),
            ("a~1", "want letters, digits, ., _, / or -"),
            ("a:b", "want letters, digits, ., _, / or -"),
            ("a@{1}", "want letters, digits, ., _, / or -"),
            ("é", "want letters, digits, ., _, / or -"),
            ("-x", "starts with -"),
            ("--upload-pack", "starts with -"),
            ("HEAD", "want a branch or tag name, not a full ref"),
            ("refs/pull/1/head", "want a branch or tag name, not a full ref"),
            ("refs/heads/main", "want a branch or tag name, not a full ref"),
            ("abc1234", "looks like a commit SHA"),
            ("0123456789abcdef0123456789ABCDEF01234567", "looks like a commit SHA"),
            ("a..b", "not a valid git ref name"),
            ("a//b", "not a valid git ref name"),
            ("/a", "not a valid git ref name"),
            ("a/", "not a valid git ref name"),
            ("a.", "not a valid git ref name"),
            (".a", "not a valid git ref name"),
            ("a/.b", "not a valid git ref name"),
            ("x.lock", "not a valid git ref name"),
            ("x.lock/y", "not a valid git ref name"),
        ):
            with self.subTest(ref=ref):
                with self.assertRaises(Invalid) as caught:
                    check_ref(ref)
                self.assertEqual(str(caught.exception), f"unsafe ref {ref[:80]!r}: {why}")


if __name__ == "__main__":
    unittest.main()
