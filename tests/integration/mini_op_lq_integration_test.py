""".mini-op/runbooks/test-accounts/lq under zsh, with a fake keychain library and a fake curl:
never the real keychain or Linear.
"""

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from tests.case import REPO, Case

LQ = Path(REPO, ".mini-op/runbooks/test-accounts/lq")
TOKEN = "lin_oauth_SENTINEL_token"
KEYCHAIN = "keychain_has() { [[ -f $HOME/keychain/$1 ]]; }\nkeychain_read() { cat -- $HOME/keychain/$1; }\n"
CURL = (
    "#!/bin/sh\n"
    'printf "%s\\n" "$@" > "$HOME/curl.argv"\n'
    'cat > "$HOME/curl.stdin"\n'
    'cat "$HOME/reply"\n'
    'exit "$(cat "$HOME/curl.rc")"\n'
)
ERRORS = {
    "errors": [
        {
            "message": "You are not allowed to create workflow states for this team",
            "extensions": {"code": "FORBIDDEN", "statusCode": 403},
        },
        {"message": 'second "quoted" error'},
    ],
    "data": None,
}


def zsh():
    path = shutil.which("zsh")
    if path:
        return path
    if os.environ.get("GITHUB_ACTIONS"):
        raise AssertionError("CI must install zsh")
    raise unittest.SkipTest("zsh not on PATH")


class Lq(Case):
    def setUp(self):
        self.root = Path(self.home)
        (self.root / "apm/lib").mkdir(parents=True)
        (self.root / "apm/lib/keychain.zsh").write_text(KEYCHAIN)
        (self.root / "keychain").mkdir()
        record = {"access_token": TOKEN, "expires_at": "2099-01-01T00:00:00Z"}
        (self.root / "keychain/svc-token").write_text(json.dumps(record))
        (self.root / "bin").mkdir()
        (self.root / "bin/curl").write_text(CURL)
        (self.root / "bin/curl").chmod(0o700)
        (self.root / "body.json").write_text('{"query": "{ viewer { id } }"}')

    def lq(self, reply, rc=0):
        (self.root / "reply").write_text(reply if isinstance(reply, str) else json.dumps(reply))
        (self.root / "curl.rc").write_text(f"{rc}\n")
        path = f"{self.root / 'bin'}{os.pathsep}{os.environ.get('PATH', os.defpath)}"
        return subprocess.run(
            [zsh(), "-f", str(LQ), "svc-token", "svc-token", str(self.root / "body.json")],
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": path},
            timeout=30,
            check=False,
        )

    def assert_no_token(self, done):
        self.assertNotIn(TOKEN, done.stdout + done.stderr)
        self.assertNotIn(TOKEN, (self.root / "curl.argv").read_text())

    def test_graphql_errors_exit_1_with_their_messages(self):
        done = self.lq(ERRORS)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertEqual(
            done.stderr.splitlines(),
            [
                "lq: Linear error: You are not allowed to create workflow states for this team",
                'lq: Linear error: second \\"quoted\\" error',
            ],
        )
        self.assertEqual(json.loads(done.stdout), ERRORS)
        self.assert_no_token(done)

    def test_errors_without_a_message_still_exit_1(self):
        done = self.lq({"errors": [{"extensions": {"code": "INTERNAL"}}]})
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stderr, "lq: Linear answered with errors\n")

    def test_data_reply_exits_0_and_prints_it(self):
        reply = {"data": {"viewer": {"id": "app-user", "errors": "a field, not GraphQL errors"}}}
        done = self.lq(reply)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), reply)
        self.assertEqual(done.stderr, "")
        self.assertEqual((self.root / "curl.stdin").read_text(), f"Authorization: Bearer {TOKEN}\n")
        self.assert_no_token(done)

    def test_curl_failure_exits_1(self):
        done = self.lq('{"errors":[{"message":"Authentication required"}]}', rc=22)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stderr, "lq: curl exited 22\n")
        self.assert_no_token(done)
