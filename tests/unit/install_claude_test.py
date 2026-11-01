"""The installer's embedded Claude account programs, without running the shell."""

import io
import json
import sys
from contextlib import redirect_stdout
from unittest import mock

from tests.case import Case
from tests.unit.install_script import program


def run(function, stdin, *argv):
    """function's program on stdin with argv: (stdout lines, exit message or None)."""
    out = io.StringIO()
    with (
        mock.patch.object(sys, "argv", ["-c", *argv]),
        mock.patch.object(sys, "stdin", io.StringIO(stdin)),
        redirect_stdout(out),
    ):
        try:
            exec(program(function), {})
        except SystemExit as stop:
            return out.getvalue().splitlines(), str(stop.code)
    return out.getvalue().splitlines(), None


class ClaudeAccounts(Case):
    def test_each_registered_account_is_a_name_and_dir_line(self):
        accounts = {"home": {"CLAUDE_CONFIG_DIR": "/h/home"}, "work": {"CLAUDE_CONFIG_DIR": "/h/w"}}
        self.assertEqual(
            run("claude_accounts_of", json.dumps(accounts)), (["home\t/h/home", "work\t/h/w"], None)
        )
        self.assertEqual(run("claude_accounts_of", "{}"), ([], None))

    def test_a_dir_with_a_tab_or_newline_is_refused(self):
        for folder in ("/h/a\tb", "/h/a\nb"):
            with self.subTest(folder=folder):
                accounts = {"work": {"CLAUDE_CONFIG_DIR": folder}}
                lines, stop = run("claude_accounts_of", json.dumps(accounts))
                self.assertEqual(lines, [])
                self.assertIn("work", stop or "")


class BrokerNeed(Case):
    def test_a_claude_account_is_ready_only_when_its_own_credential_is_usable(self):
        status = {"claude": {"work": True, "home": False, "default": True}}
        for name, want in (("work", "ready"), ("home", "login"), ("other", "login")):
            with self.subTest(name=name):
                self.assertEqual(
                    run("broker_need", json.dumps(status), "claude", name), ([want], "0")
                )
        self.assertEqual(
            run("broker_need", json.dumps({"claude": {}}), "claude", "work"), (["login"], "0")
        )
