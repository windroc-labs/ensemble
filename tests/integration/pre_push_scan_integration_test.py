"""src/broker/internal/launcher/assets/pre-push-scan against real git repos and local bare remotes."""

import builtins
import contextlib
import hashlib
import io
import os
import runpy
import shutil
import subprocess
from pathlib import Path
from unittest import mock

from tests.case import REPO, Case

ASSETS = Path(REPO, "src/broker/internal/launcher/assets")
HOOK = ASSETS / "pre-push-scan"
SCANNER_SHA256 = "75acf1514caee7440ce6959f7f3329374f06c3148dcc76ecede8e64a01a5eadc"
NAMES_FILE = "/run/ensemble/secrets/env/repo-guards-private-names"
# Built so this file never holds a home path itself.
HOME_PATH = "/" + "home/someone/project/x.py"
TRACKER_ID = "ABC-123"
NAMES = "acme-secret\nwidgetco\n"
GITHUB_URL = "https://github.com/acme/widget.git"


class PrePushScan(Case):
    def setUp(self):
        self.hooks = Path(self.home, "hooks")
        self.hooks.mkdir()
        shutil.copy(HOOK, self.hooks / "pre-push")
        for name in ("repo-guards-scan", "githook-forward"):
            shutil.copy(ASSETS / name, self.hooks)
        # Command scope, as the worker image's GIT_CONFIG_* environment sets it: above every config file.
        self.hooks_path = ("-c", f"core.hooksPath={self.hooks}")
        self.env = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "a",
            "GIT_AUTHOR_EMAIL": "a@example.com",
            "GIT_COMMITTER_NAME": "a",
            "GIT_COMMITTER_EMAIL": "a@example.com",
            "REPO_GUARDS_PRIVATE_NAMES": NAMES,
        }
        self.remote = Path(self.home, "remote.git")
        self.work = Path(self.home, "work")
        self.git(self.home, "init", "-q", "--bare", str(self.remote))
        self.git(self.home, "init", "-q", "-b", "main", str(self.work))
        self.git(self.work, "remote", "add", "origin", str(self.remote))
        # The in-process hook's ls-remote of GITHUB_URL reaches the local bare remote.
        self.git(self.work, "config", f"url.{self.remote}.insteadOf", GITHUB_URL)
        (self.work / ".git/info/exclude").write_text(".githooks/\n.husky/\nmarker*\n")
        self.commit({"README.md": "hello\n"}, "start")
        self.push()

    def git(self, cwd, *args, env=None):
        return subprocess.run(
            ["git", *self.hooks_path, *args],
            cwd=cwd,
            env=env or self.env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def commit(self, files, message, env=None):
        for path, content in files.items():
            target = self.work / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content)
        self.git(self.work, "add", "-A")
        self.git(self.work, "commit", "-q", "--allow-empty", "-m", message, env=env)
        return self.head()

    def head(self):
        return self.git(self.work, "rev-parse", "HEAD").strip()

    def push(self, *refspec, remote="origin"):
        return subprocess.run(
            ["git", *self.hooks_path, "push", remote, *(refspec or ["HEAD:refs/heads/main"])],
            cwd=self.work,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def unchecked_push(self, *refspec):
        self.git(self.work, "push", "-q", "--no-verify", "origin", *refspec)

    def refused(self, proc, *checks):
        self.assertNotEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("pre-push: refused", proc.stderr)
        for check in checks:
            self.assertRegex(proc.stderr, rf": {check} \(leak\)")
        self.assertIn("fix: ", proc.stderr)
        self.assertNoSecrets(proc.stderr)

    def assertNoSecrets(self, text):
        for secret in (HOME_PATH, "someone", "widgetco", "acme-secret", "personal.dev"):
            self.assertNotIn(secret, text)

    def run_hook(self, url, updates, gh=None):
        """The hook in-process from the work tree, `gh` answered by gh(argv) -> CompletedProcess."""
        code = runpy.run_path(str(self.hooks / "pre-push"), run_name="pre_push_scan")
        real_run = subprocess.run

        def fake_run(argv, *args, **kwargs):
            if argv[0] == "gh":
                if gh is None:
                    raise FileNotFoundError("gh")
                return gh(argv)
            return real_run(argv, *args, **kwargs)

        err = io.StringIO()
        with (
            mock.patch.dict(os.environ, self.env, clear=True),
            mock.patch("subprocess.run", side_effect=fake_run),
            contextlib.redirect_stderr(err),
            contextlib.chdir(self.work),
        ):
            status = code["main"](["origin", url], updates.encode())
        return status, err.getvalue()

    def update_line(self, sha, remote_sha="0" * 40):
        return f"refs/heads/main {sha} refs/heads/main {remote_sha}\n"

    def answer(self, visibility):
        return lambda argv: subprocess.CompletedProcess(argv, 0, visibility + "\n", "")

    def test_vendored_scanner_is_the_pinned_upstream_file(self):
        digest = hashlib.sha256((ASSETS / "repo-guards-scan").read_bytes()).hexdigest()
        self.assertEqual(digest, SCANNER_SHA256)

    def test_clean_commit_is_pushed(self):
        self.commit({"src/app.txt": "plain text\n"}, "Add app")
        proc = self.push()
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_home_path_in_a_file_is_refused_without_printing_it(self):
        self.commit({"notes.txt": f"built in {HOME_PATH}\n"}, "Add notes")
        proc = self.push()
        self.refused(proc, "local-path")
        self.assertIn("file #1:1:10: local-path (leak)", proc.stderr)
        self.assertNotIn("notes.txt", proc.stderr)

    def test_tracker_id_not_in_the_list_is_pushed(self):
        self.commit({"a.txt": f"see {TRACKER_ID}\n"}, f"Fix the thing ({TRACKER_ID})")
        proc = self.push()
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_private_name_in_a_message_is_refused(self):
        sha = self.commit({}, "Fix the widgetco thing")
        proc = self.push()
        self.refused(proc, "private-name")
        self.assertIn(f"commit {sha} line 1: private-name (leak)", proc.stderr)

    def test_private_name_and_personal_email_are_refused(self):
        self.commit({"a.txt": "by acme-secret, mail me at a@personal.dev\n"}, "Add a")
        self.refused(self.push(), "private-name", "email")

    def test_author_email_and_name_are_refused(self):
        env = {**self.env, "GIT_AUTHOR_EMAIL": "me@personal.dev", "GIT_AUTHOR_NAME": "widgetco bot"}
        sha = self.commit({}, "Empty", env=env)
        proc = self.push()
        self.refused(proc, "email", "private-name")
        self.assertIn(f"commit {sha} author: email (leak)", proc.stderr)
        self.assertIn(f"commit {sha} author name: private-name (leak)", proc.stderr)

    def test_junk_paths_are_refused(self):
        for path in (
            "pkg/__pycache__/m.cpython-311.pyc",
            "web/node_modules/x/index.js",
            "m.pyc",
            "dist/pkg-1.0-py3-none-any.whl",
            "pkg.egg-info/PKG-INFO",
        ):
            with self.subTest(path=path):
                self.git(self.work, "reset", "-q", "--hard", "origin/main")
                self.commit({path: "x\n"}, "Add junk")
                self.refused(self.push(), "junk")

    def test_binary_blob_with_an_embedded_home_path_is_refused(self):
        self.commit({"m.bin": b"\x00\xff\xfe" + HOME_PATH.encode() + b"\x00\x80"}, "Add binary")
        proc = self.push()
        self.refused(proc, "local-path")
        self.assertIn("skipped non-text files: 0", proc.stderr)

    def test_leak_added_then_removed_before_the_push_is_refused(self):
        self.commit({"notes.txt": HOME_PATH + "\n"}, "Add notes")
        self.commit({"notes.txt": "clean\n"}, "Clean notes")
        self.refused(self.push(), "local-path")

    def test_a_blob_shared_by_several_commits_reports_its_first_occurrence(self):
        self.commit({"a.txt": HOME_PATH + "\n"}, "Add a")
        self.commit({"b.txt": HOME_PATH + "\n"}, "Add b")
        proc = self.push()
        self.refused(proc, "local-path")
        self.assertIn("total hits: 1", proc.stderr)

    def test_content_the_destination_has_is_not_rescanned(self):
        self.commit({"notes.txt": HOME_PATH + "\n"}, "Add notes")
        self.unchecked_push("HEAD:refs/heads/main")
        self.commit({"other.txt": "clean\n"}, "Add other")
        for refspec in ("HEAD:refs/heads/main", "HEAD:refs/heads/topic"):
            with self.subTest(refspec):
                proc = self.push(refspec)
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_push_by_url_skips_what_the_destination_has(self):
        self.commit({"notes.txt": HOME_PATH + "\n"}, "Add notes")
        self.unchecked_push("HEAD:refs/heads/main")
        self.commit({"other.txt": "clean\n"}, "Add other")
        proc = self.push("HEAD:refs/heads/topic", remote=str(self.remote))
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_commits_only_the_fetch_side_has_are_scanned(self):
        self.commit({"notes.txt": HOME_PATH + "\n"}, "Add notes")
        self.unchecked_push("HEAD:refs/heads/main")
        public = Path(self.home, "public.git")
        self.git(self.home, "init", "-q", "--bare", str(public))
        self.git(self.work, "remote", "set-url", "--push", "origin", str(public))
        self.git(self.work, "fetch", "-q", "origin")
        self.refused(self.push(), "local-path")

    def test_merge_scans_only_what_the_merge_itself_adds(self):
        self.git(self.work, "checkout", "-q", "-b", "side")
        self.commit({"side.txt": HOME_PATH + "\n"}, "Side")
        self.unchecked_push("side")
        self.git(self.work, "checkout", "-q", "main")
        self.commit({"main.txt": "clean\n"}, "Main")
        self.git(self.work, "merge", "-q", "--no-edit", "side")
        proc = self.push()
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_annotated_tag_message_and_tagger_are_scanned(self):
        env = {**self.env, "GIT_COMMITTER_EMAIL": "me@personal.dev"}
        self.git(self.work, "tag", "-a", "v1", "-m", f"built in {HOME_PATH}", env=env)
        tag = self.git(self.work, "rev-parse", "v1").strip()
        proc = self.push("refs/tags/v1")
        self.refused(proc, "local-path", "email")
        self.assertIn(f"tag {tag} message line 1: local-path (leak)", proc.stderr)
        self.assertIn(f"tag {tag} tagger line 1: email (leak)", proc.stderr)

    def test_clean_annotated_tag_is_pushed(self):
        self.git(self.work, "tag", "-a", "v1", "-m", "Release 1")
        proc = self.push("refs/tags/v1")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_pushed_tree_is_refused(self):
        proc = self.push("HEAD^{tree}:refs/tags/t")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("leak scan failed", proc.stderr)

    def test_branch_deletion_is_allowed(self):
        self.assertEqual(self.push("HEAD:refs/heads/topic").returncode, 0)
        proc = self.push(":refs/heads/topic")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_repository_hooks_path_cannot_skip_the_scan(self):
        self.git(self.work, "config", "core.hooksPath", ".husky")
        self.commit({"notes.txt": HOME_PATH + "\n"}, "Add notes")
        self.refused(self.push(), "local-path")

    def test_repository_pre_push_hook_runs_with_the_push_arguments_only_after_a_clean_scan(self):
        for hooks_dir, config in ((".githooks", None), (".husky", ".husky")):
            with self.subTest(hooks_dir):
                if config:
                    self.git(self.work, "config", "core.hooksPath", config)
                (self.work / hooks_dir).mkdir()
                # touch marks that the hook ran: it creates a file named by its first argument.
                shutil.copy("/usr/bin/touch", self.work / hooks_dir / "pre-push")
                self.git(self.work, "remote", "add", "marker", str(self.remote))
                self.commit({"notes.txt": HOME_PATH + "\n"}, "Add notes")
                self.refused(self.push(remote="marker"), "local-path")
                self.assertFalse((self.work / "marker").exists())
                self.git(self.work, "reset", "-q", "--hard", "HEAD~1")
                self.commit({"clean.txt": "x\n"}, "Clean")
                self.assertEqual(self.push(remote="marker").returncode, 0)
                self.assertTrue((self.work / "marker").exists())
                (self.work / "marker").unlink()
                self.git(self.work, "remote", "remove", "marker")
                shutil.rmtree(self.work / hooks_dir)

    def test_a_failing_repository_pre_push_hook_refuses(self):
        (self.work / ".githooks").mkdir()
        shutil.copy("/bin/false", self.work / ".githooks/pre-push")
        self.commit({"clean.txt": "x\n"}, "Clean")
        proc = self.push()
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("pre-push: refused", proc.stderr)

    def test_missing_list_refuses_a_public_push(self):
        self.env = {k: v for k, v in self.env.items() if k != "REPO_GUARDS_PRIVATE_NAMES"}
        self.commit({"src/app.txt": "plain\n"}, "Add app")
        real_isfile = os.path.isfile
        with mock.patch(
            "os.path.isfile",
            side_effect=lambda p: False if p == NAMES_FILE else real_isfile(p),
        ):
            status, err = self.run_hook(GITHUB_URL, self.update_line(self.head()))
        self.assertEqual(status, 1)
        self.assertIn("private-name list unavailable", err)

    def run_with_names_file(self, content):
        """run_hook with the run secret file at NAMES_FILE holding content."""
        served = Path(self.home, "names")
        served.write_text(content)
        real_isfile, real_open = os.path.isfile, builtins.open

        def isfile(path):
            return True if path == NAMES_FILE else real_isfile(path)

        def open_(path, *args, **kwargs):
            return real_open(served if path == NAMES_FILE else path, *args, **kwargs)

        with mock.patch("os.path.isfile", side_effect=isfile), mock.patch("builtins.open", open_):
            return self.run_hook(GITHUB_URL, self.update_line(self.head()))

    def test_the_run_secret_file_supplies_the_list(self):
        self.env = {k: v for k, v in self.env.items() if k != "REPO_GUARDS_PRIVATE_NAMES"}
        self.commit({}, "Fix the zorblax thing")
        status, err = self.run_with_names_file("zorblax\n")
        self.assertEqual(status, 1)
        self.assertIn("private-name (leak)", err)

    def test_the_environment_list_wins_over_the_run_secret_file(self):
        self.commit({}, "Fix the zorblax thing")
        status, err = self.run_with_names_file("zorblax\n")
        self.assertEqual((status, err), (0, ""))

    def test_private_and_internal_repositories_skip_the_scan(self):
        self.commit({"notes.txt": HOME_PATH + "\n"}, "Fix the widgetco thing")
        for visibility in ("private", "internal"):
            with self.subTest(visibility):
                calls = []

                def gh(argv, visibility=visibility, calls=calls):
                    calls.append(argv)
                    return self.answer(visibility)(argv)

                status, err = self.run_hook(GITHUB_URL, self.update_line(self.head()), gh)
                self.assertEqual((status, err), (0, ""))
                self.assertEqual(
                    calls,
                    [
                        [
                            "gh",
                            "api",
                            "--hostname",
                            "github.com",
                            "repos/acme/widget",
                            "--jq",
                            ".visibility",
                        ]
                    ],
                )

    def test_visibility_other_than_private_scans(self):
        self.commit({}, "Fix the widgetco thing")
        line = self.update_line(self.head())
        answers = {
            "public": self.answer("public"),
            "error": lambda argv: subprocess.CompletedProcess(argv, 1, "", "HTTP 404"),
            "odd": self.answer(""),
            "no gh": None,
        }
        for name, gh in answers.items():
            with self.subTest(name):
                status, err = self.run_hook(GITHUB_URL, line, gh)
                self.assertEqual(status, 1)
                self.assertIn("private-name (leak)", err)

    def test_github_urls_are_looked_up(self):
        code = runpy.run_path(str(HOOK), run_name="pre_push_scan")
        for url in (
            "https://github.com/acme/widget.git",
            "https://x-access-token@github.com/acme/widget",
            "git@github.com:acme/widget.git",
            "ssh://git@github.com/acme/widget.git",
        ):
            with self.subTest(url):
                self.assertEqual(code["GITHUB"].fullmatch(url).groups(), ("acme", "widget"))
        for url in ("https://gitlab.com/acme/widget.git", "/srv/git/widget.git"):
            with self.subTest(url):
                self.assertIsNone(code["GITHUB"].fullmatch(url))

    def test_scan_failure_refuses(self):
        status, err = self.run_hook(str(self.remote), "not a ref line\n")
        self.assertEqual(status, 1)
        self.assertIn("leak scan failed", err)
