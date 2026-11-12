"""src/broker/internal/launcher/assets/pre-push-scan against real git repos and a local bare remote."""

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
# Built so this file never holds a home path or a tracker id itself.
HOME_PATH = "/" + "home/someone/project/x.py"
TRACKER_ID = "AB" + "C-" + "123"
NAMES = "acme-secret\nwidgetco\n"
GITHUB_URL = "https://github.com/acme/widget.git"


class PrePushScan(Case):
    def setUp(self):
        self.env = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "a",
            "GIT_AUTHOR_EMAIL": "a@example.com",
            "GIT_COMMITTER_NAME": "a",
            "GIT_COMMITTER_EMAIL": "a@example.com",
            "REPO_GUARDS_PRIVATE_NAMES": NAMES,
        }
        self.hooks = Path(self.home, "hooks")
        self.hooks.mkdir()
        shutil.copy(HOOK, self.hooks / "pre-push")
        shutil.copy(ASSETS / "repo-guards-scan", self.hooks)
        self.remote = Path(self.home, "remote.git")
        self.work = Path(self.home, "work")
        self.git(self.home, "init", "-q", "--bare", str(self.remote))
        self.git(self.home, "init", "-q", "-b", "main", str(self.work))
        self.git(self.work, "config", "core.hooksPath", str(self.hooks))
        self.git(self.work, "remote", "add", "origin", str(self.remote))
        self.commit({"README.md": "hello\n"}, "start")
        self.push()

    def git(self, cwd, *args, env=None):
        return subprocess.run(
            ["git", *args], cwd=cwd, env=env or self.env, capture_output=True, text=True, check=True
        ).stdout

    def commit(self, files, message):
        for path, content in files.items():
            target = self.work / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content)
        self.git(self.work, "add", "-A")
        self.git(self.work, "commit", "-q", "--allow-empty", "-m", message)
        return self.git(self.work, "rev-parse", "HEAD").strip()

    def push(self, *refspec, env=None):
        return subprocess.run(
            ["git", "push", "origin", *(refspec or ["HEAD:refs/heads/main"])],
            cwd=self.work,
            env=env or self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def refused(self, proc, *checks):
        self.assertNotEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("pre-push: refused", proc.stderr)
        for check in checks:
            self.assertRegex(proc.stderr, rf": {check} \(leak\)")
        self.assertIn("fix: ", proc.stderr)

    def assertNoSecrets(self, text):
        for secret in (HOME_PATH, "someone", "widgetco", "acme-secret"):
            self.assertNotIn(secret, text)

    def run_hook(self, url, updates, gh=None):
        """The hook in-process from the work tree, `gh` answered by gh(argv) -> CompletedProcess."""
        code = runpy.run_path(str(HOOK), run_name="pre_push_scan")
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
        self.assertNoSecrets(proc.stderr)
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
        self.assertNoSecrets(proc.stderr)

    def test_private_name_and_personal_email_are_refused(self):
        self.commit({"a.txt": "by acme-secret, mail me at a@personal.dev\n"}, "Add a")
        proc = self.push()
        self.refused(proc, "private-name", "email")
        self.assertNotIn("personal.dev", proc.stderr)

    def test_author_email_is_refused(self):
        env = {**self.env, "GIT_AUTHOR_EMAIL": "me@personal.dev"}
        self.git(self.work, "commit", "-q", "--allow-empty", "-m", "Empty", env=env)
        proc = self.push()
        self.refused(proc, "email")
        self.assertRegex(proc.stderr, r"commit [0-9a-f]{40} author: email")

    def test_junk_paths_are_refused(self):
        for path in ("pkg/__pycache__/m.cpython-311.pyc", "web/node_modules/x/index.js", "m.pyc"):
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

    def test_content_already_on_the_remote_is_not_rescanned(self):
        self.commit({"notes.txt": HOME_PATH + "\n"}, "Add notes")
        self.git(self.work, "push", "-q", "--no-verify", "origin", "HEAD:refs/heads/main")
        self.commit({"other.txt": "clean\n"}, "Add other")
        proc = self.push()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.push("HEAD:refs/heads/topic")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_merge_scans_only_what_the_merge_itself_adds(self):
        self.git(self.work, "checkout", "-q", "-b", "side")
        self.commit({"side.txt": HOME_PATH + "\n"}, "Side")
        self.git(self.work, "push", "-q", "--no-verify", "origin", "side")
        self.git(self.work, "checkout", "-q", "main")
        self.commit({"main.txt": "clean\n"}, "Main")
        self.git(self.work, "merge", "-q", "--no-edit", "side")
        proc = self.push()
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_branch_deletion_is_allowed(self):
        self.assertEqual(self.push("HEAD:refs/heads/topic").returncode, 0)
        proc = self.push(":refs/heads/topic")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_missing_list_refuses_a_public_push(self):
        env = {k: v for k, v in self.env.items() if k != "REPO_GUARDS_PRIVATE_NAMES"}
        self.commit({"src/app.txt": "plain\n"}, "Add app")
        real_isfile = os.path.isfile
        hook_names_file = "/run/ensemble/secrets/env/repo-guards-private-names"
        self.env = env
        with mock.patch(
            "os.path.isfile",
            side_effect=lambda p: False if p == hook_names_file else real_isfile(p),
        ):
            status, err = self.run_hook(GITHUB_URL, self.update_line(self.head()))
        self.assertEqual(status, 1)
        self.assertIn("private-name list unavailable", err)

    def head(self):
        return self.git(self.work, "rev-parse", "HEAD").strip()

    def test_private_repository_skips_the_scan(self):
        self.commit({"notes.txt": HOME_PATH + "\n"}, "Fix the widgetco thing")
        calls = []

        def gh(argv):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "private\n", "")

        status, err = self.run_hook(GITHUB_URL, self.update_line(self.head()), gh)
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(calls, [["gh", "api", "repos/acme/widget", "--jq", ".visibility"]])

    def test_visibility_other_than_private_scans(self):
        self.commit({}, "Fix the widgetco thing")
        line = self.update_line(self.head())
        answers = {
            "public": lambda argv: subprocess.CompletedProcess(argv, 0, "public\n", ""),
            "error": lambda argv: subprocess.CompletedProcess(argv, 1, "", "HTTP 404"),
            "odd": lambda argv: subprocess.CompletedProcess(argv, 0, "\n", ""),
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

    def test_repository_pre_push_hook_runs_after_a_clean_scan(self):
        repo_hooks = self.work / ".githooks"
        repo_hooks.mkdir()
        (self.work / ".git/info/exclude").write_text(".githooks/\n")
        shutil.copy("/bin/false", repo_hooks / "pre-push")
        self.commit({"src/app.txt": "plain\n"}, "Add app")
        proc = self.push()
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("pre-push: refused", proc.stderr)
        shutil.copy("/bin/true", repo_hooks / "pre-push")
        self.assertEqual(self.push().returncode, 0)

    def test_scan_failure_refuses(self):
        status, err = self.run_hook(str(self.remote), "not a ref line\n")
        self.assertEqual(status, 1)
        self.assertIn("leak scan failed", err)
