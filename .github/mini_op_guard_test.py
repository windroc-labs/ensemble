""".github/mini_op_guard.py over temp trees and fake pull_request events; git is faked."""

import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import mini_op_guard as guard

BASE = "a" * 40
HEAD = "b" * 40
SHA = "c" * 40
# Invented samples, one per pattern class.
SAMPLES = {
    "private IPv4": [
        "ssh admin@10.20.30.40",
        "url=http://172.20.1.2:9000/api",
        "host 192.168.77.5",
        "tailnet 100.70.1.1",
        "padded 10.20.30.040",
        "padded 192.168.001.002",
    ],
    "email address": ["mail someone@example.org now"],
    "UUID": [
        "team = 0f8e2c4a-1b3d-4e5f-9a7b-6c8d0e2f4a6b",
        "ID 0F8E2C4A-1B3D-4E5F-9A7B-6C8D0E2F4A6B",
    ],
    ".local host name": ["ping box.local", "open vnc://Box-Mini.local:5900"],
    "keychain item literal": [
        "keychain_read made-up-item made-up-account | ans",
        'keychain_has "made-up-item"',
        "security find-generic-password -s made-up-item -w",
        "printf 'add-generic-password -U -s made-up-item -w x\\n' |",
        "~/mini-op-projects/ensemble/test-accounts/storekey made-up-item acct",
        "~/mini-op-projects/ensemble/install-ensemble/storekey-stdin made-up-item acct",
        "~/mini-op-projects/ensemble/test-accounts/lq made-up-item acct body.json",
        "importkey $d/export.keychain-db made-up-item acct",
        "ssh mini 'security find-generic-password -s made-up-item'",
        "printf 'add-generic-password -s made-up-item' | security -i",
        "Check with `keychain_has made-up-item`.",
        "ssh mini 'source ~/apm/lib/keychain.zsh; keychain_has made-up-item'",
        "if keychain_has made-up-item; then",
        "  lq made-up-item acct body.json",
        "if ! keychain_has made-up-item; then",
        "while ! keychain_has made-up-item; do",
        "{ keychain_read made-up-item; }",
        "ssh mini keychain_has made-up-item",
        "sudo keychain_read made-up-item acct",
        "REF=x lq made-up-item acct b.json",
        "- keychain_read made-up-item acct",
        "1. keychain_read made-up-item acct",
        "keychain_read made-up-item>out",
        "keychain_read item2 acct",
        "storekey writes it, then keychain_read made-up-item acct checks it",
        "The lq step calls keychain_read made-up-item acct",
    ],
}
CLEAN = [
    "ssh -l user host",
    "ls -l ~/.local/share",
    "the mini's ~/.ensemble/config.local.toml and config.local.yaml",
    "keychain_read <service> <account> | ans",
    'keychain_read "$s" "$a" "$kc"',
    'security find-generic-password -s "$1" -a "$2" -w',
    'printf \'add-generic-password -U -s %s -a %s -w "%s"\\n\' "$ns" "$na" "$value" |',
    "importkey $d/export.keychain-db <service> <account>",
    "localhost 127.0.0.1 and 8.8.8.8, version 10.20.30.40.5",
    "npm install pkg@1.2.3 and @scope/name",
    "Co-Authored-By: Claude <noreply@anthropic.com>",
    "<user>@<address>",
    "build 1.08.0.1 and 999.1.1.1",
    "to security, keychain_read or the project's key scripts.",
    "per-runbook dirs: install-test, clean-test, importkey, lq and storekey,",
    "plus storekey-stdin for the Strata key and the templates",
    "Board extras, with the same `lq` call (setup key)",
    "lq reads the body",
    "and then storekey asks for the key",
    'the "lq and storekey" scripts',
    "keychain_read <service> <account> > /tmp/x",
    "lq and storekey-stdin.",
    "run storekey and storekey-stdin first",
]


class Tree(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def run_guard(self, environ=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = guard.main([], root=self.root, environ=environ or {})
        return code, out.getvalue()


class GuardTest(Tree):
    def test_real_tree_clean(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = guard.main([], environ={})
        self.assertEqual(code, 0, out.getvalue())
        self.assertRegex(out.getvalue(), r"mini-op guard: \d+ files clean")

    def test_no_mini_op_dir_is_clean(self):
        self.assertEqual(self.run_guard(), (0, "mini-op guard: 0 files clean\n"))

    def test_each_class_in_file(self):
        for kind, lines in SAMPLES.items():
            for line in lines:
                with self.subTest(line=line):
                    self.write(".mini-op/runbooks/x.md", f"---\nwhen: x\n---\n{line}\n")
                    code, out = self.run_guard()
                    self.assertEqual(code, 1)
                    self.assertIn(f".mini-op/runbooks/x.md:4: {kind}\n", out)
                    self.assertNotIn(line, out)
                    self.assertIn("mini-op guard: 1 findings", out)

    def test_clean_lines_pass(self):
        self.write(".mini-op/runbooks/x.md", "\n".join(CLEAN) + "\n")
        code, out = self.run_guard()
        self.assertEqual(code, 0, out)
        self.assertIn("mini-op guard: 1 files clean", out)

    def test_continued_line_is_joined(self):
        self.write(".mini-op/runbooks/x.md", "ok\nkeychain_read \\\n  made-up-item acct\n")
        code, out = self.run_guard()
        self.assertEqual(code, 1)
        self.assertIn(".mini-op/runbooks/x.md:2: keychain item literal\n", out)

    def test_flagged_name_redacted(self):
        self.write(".mini-op/runbooks/a.md", "fine\n")
        self.write(".mini-op/runbooks/10.20.30.40/notes", "fine\n")
        self.write(".mini-op/runbooks/someone@example.org.md", "fine\n")
        code, out = self.run_guard()
        self.assertEqual(code, 1)
        self.assertNotIn("10.20.30.40", out)
        self.assertNotIn("someone@example.org", out)
        self.assertIn(".mini-op/<entry #1>: private IPv4\n", out)
        self.assertIn(".mini-op/<entry #3>: email address\n", out)
        self.assertNotIn("a.md", out)

    def test_mini_op_as_a_file_fails_closed(self):
        self.write(".mini-op", "fine\n")
        code, out = self.run_guard()
        self.assertEqual(code, 1)
        self.assertIn(".mini-op: not a directory\n", out)

    def test_symlink_and_binary_fail_closed(self):
        self.write(".mini-op/runbooks/x/target", "fine\n")
        os.symlink("target", self.root / ".mini-op/runbooks/x/link")
        (self.root / ".mini-op/runbooks/x/blob").write_bytes(b"ok\x00ok")
        (self.root / ".mini-op/runbooks/x/latin").write_bytes(b"caf\xe9\n")
        code, out = self.run_guard()
        self.assertEqual(code, 1)
        self.assertIn(".mini-op/runbooks/x/link: not a regular file\n", out)
        self.assertIn(".mini-op/runbooks/x/blob: not UTF-8 text\n", out)
        self.assertIn(".mini-op/runbooks/x/latin: not UTF-8 text\n", out)


class FakeGit:
    def __init__(self, changed=(".mini-op/runbooks/x.md",), commits=(), fail=None):
        self.calls = []
        self.changed = changed
        self.commits = commits
        self.fail = fail

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        command = argv[3]
        if command == self.fail:
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"fatal: secret detail")
        if command == "diff":
            out = b"".join(path.encode() + b"\0" for path in self.changed)
        else:
            out = b"".join(f"{sha}\x1f{body}\0".encode() for sha, body in self.commits)
        return SimpleNamespace(returncode=0, stdout=out, stderr=b"")


class PullRequestTest(Tree):
    def event(self, title="Move runbooks", body="Moves them.", base=BASE, head=HEAD, name="event"):
        path = self.root / f"{name}.json"
        pr = {"title": title, "body": body, "base": {"sha": base}, "head": {"sha": head}}
        path.write_text(json.dumps({"pull_request": pr}))
        return {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": str(path)}

    def run_with(self, git, environ):
        with mock.patch.object(guard.subprocess, "run", git):
            return self.run_guard(environ)

    def test_pr_fields_rejected(self):
        for kind, line in (
            ("private IPv4", "see 10.20.30.40"),
            ("email address", "ask someone@example.org"),
            ("keychain item literal", "keychain_read made-up-item acct"),
        ):
            for field in ("title", "body", "commit"):
                with self.subTest(kind=kind, field=field):
                    git = FakeGit(
                        commits=[(SHA, f"Subject\n\n{line}\n" if field == "commit" else "ok")]
                    )
                    fields = {field: line} if field != "commit" else {}
                    code, out = self.run_with(git, self.event(**fields))
                    self.assertEqual(code, 1, out)
                    label = {
                        "title": "PR title:1",
                        "body": "PR body:1",
                        "commit": f"commit {SHA[:12]}:3",
                    }
                    self.assertIn(f"{label[field]}: {kind}\n", out)
                    self.assertNotIn(line, out)

    def test_pr_fields_clean_after_removal(self):
        git = FakeGit(commits=[(SHA, "Subject\n\nBody.\n")])
        code, out = self.run_with(git, self.event(body="Moves them; see 10.20.30.40"))
        self.assertEqual(code, 1)
        code, out = self.run_with(git, self.event(body="Moves them."))
        self.assertEqual(code, 0, out)
        self.assertIn("PR fields clean\n", out)

    def test_pr_without_mini_op_change_skips_fields(self):
        git = FakeGit(changed=())
        code, out = self.run_with(git, self.event(body="see 10.20.30.40"))
        self.assertEqual(code, 0, out)
        self.assertIn("PR fields skipped: no .mini-op/ change\n", out)
        self.assertEqual([argv[3] for argv, _ in git.calls], ["diff"])

    def test_null_body(self):
        code, out = self.run_with(FakeGit(), self.event(body=None))
        self.assertEqual(code, 0, out)

    def test_title_is_text(self):
        title = "$(touch /tmp/pwned) `id` ; rm -rf ~"
        git = FakeGit()
        code, out = self.run_with(git, self.event(title=title))
        self.assertEqual(code, 0, out)
        for argv, kwargs in git.calls:
            self.assertIsInstance(argv, list)
            self.assertNotIn(title, " ".join(argv))
            self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(
            git.calls[0][0],
            [
                "git",
                "-C",
                str(self.root),
                "diff",
                "--name-only",
                "-z",
                f"{BASE}...{HEAD}",
                "--",
                ".mini-op/",
            ],
        )

    def test_allowed_trailer(self):
        trailer = "Subject\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"
        code, out = self.run_with(FakeGit(commits=[(SHA, trailer)]), self.event())
        self.assertEqual(code, 0, out)

    def test_push_event_scans_files_only(self):
        git = FakeGit()
        code, _ = self.run_with(
            git, {"GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": "/nonexistent"}
        )
        self.assertEqual(code, 0)
        self.assertEqual(git.calls, [])

    def test_fail_closed(self):
        bad_json = self.root / "bad.json"
        bad_json.write_text("{not json")
        no_pr = self.root / "no_pr.json"
        no_pr.write_text(json.dumps({"action": "edited"}))
        cases = [
            ({"GITHUB_EVENT_NAME": "pull_request"}, FakeGit(), "cannot read the event file"),
            (
                {
                    "GITHUB_EVENT_NAME": "pull_request",
                    "GITHUB_EVENT_PATH": str(self.root / "missing.json"),
                },
                FakeGit(),
                "cannot read the event file",
            ),
            (
                {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": str(bad_json)},
                FakeGit(),
                "malformed event",
            ),
            (
                {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": str(no_pr)},
                FakeGit(),
                "malformed event",
            ),
            (self.event(base="main; id", name="e1"), FakeGit(), "bad base or head sha"),
            (self.event(head=None, name="e2"), FakeGit(), "bad base or head sha"),
            (self.event(name="e3"), FakeGit(fail="diff"), "git diff failed"),
            (self.event(name="e4"), FakeGit(fail="log"), "git log failed"),
        ]
        for environ, git, reason in cases:
            with self.subTest(reason=reason):
                code, out = self.run_with(git, environ)
                self.assertEqual(code, 1)
                self.assertTrue(out.endswith(f"mini-op guard: {reason}\n"), out)
                self.assertNotIn("secret detail", out)
                self.assertNotIn("main; id", out)


WORKFLOW = guard.ROOT / ".github/workflows/test.yml"


def job_block(text, name):
    found = re.search(rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  \S|\Z)", text)
    assert found, f"test.yml has no job {name}"
    return found[1]


class WorkflowTest(unittest.TestCase):
    def test_triggers_and_job(self):
        text = WORKFLOW.read_text()
        self.assertRegex(
            text, r"(?m)^  pull_request:\n    types: \[opened, synchronize, reopened, edited\]$"
        )
        block = job_block(text, "mini-op")
        self.assertRegex(block, r"(?m)^    name: mini-op guard$")
        for key in ("if", "needs", "permissions"):
            self.assertNotRegex(block, rf"(?m)^    {key}:")
        self.assertIn("persist-credentials: false", block)
        self.assertIn("fetch-depth: 0", block)
        self.assertRegex(block, r"(?m)^        run: python3 \.github/mini_op_guard\.py$")
        self.assertNotIn("secrets.", block)
        self.assertNotIn("github.event.pull_request", block)
        ci = job_block(text, "ci")
        self.assertRegex(ci, r"(?m)^    needs: \[.*\bmini-op\b.*\]$")


if __name__ == "__main__":
    unittest.main()
