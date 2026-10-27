import hashlib
from pathlib import Path

from tests.case import REPO
from tests.guard_checks.keychain import keychain_free_tests
from tests.guard_samples import Sample

GO = "src/broker/internal/broker/keychain_darwin_test.go"
OLD = Path(__file__).parent / "keychain_samples/keychain_darwin_test.go.txt"
# Built from parts, so this file holds none of the patterns it checks.
ADMIN = "create" + "-keychain"
SETTINGS = "set-keychain" + "-settings"
LOGIN = "Library/" + "Keychains/login" + ".keychain-db"
FRAMEWORK = "-framework" + " Security"
# Contexts whose parentheses nest two and three levels deep.
CTX2 = "context.WithValue(context.WithValue(ctx, a, b), c, d)"
CTX3 = f"context.WithValue({CTX2}, e, f)"


def key(path, line):
    return path, hashlib.sha256(line.strip().encode()).hexdigest()


def go(body):
    return f'package broker\n\nimport "os/exec"\n\nfunc f() {{\n\t{body}\n}}\n'


class KeychainFree(Sample):
    def found(self, files, **kwargs):
        return keychain_free_tests(self.tree(files), allowed=kwargs.get("allowed", {}))

    def test_the_repo_is_keychain_free(self):
        self.assertEqual(keychain_free_tests(REPO), [])

    def test_flags_the_old_darwin_test(self):
        found = self.found({GO: OLD.read_text(encoding="utf-8")})
        expected = [
            *(f"{GO}:{line}: security-exec" for line in (25, 49, 55)),
            *(f"{GO}:{line}: keychain-admin" for line in (32, 36, 49, 55, 57, 59, 60)),
            *(f"{GO}:{line}: file-keychain" for line in (81, 118)),
        ]
        self.assertEqual(found, sorted(expected))

    def test_each_rule_flags_its_sample_only(self):
        sh = "#!/bin/sh\n"
        test_go, test_py = "src/b/a_test.go", "tests/a_test.py"
        for name, path, flagged, line, missed, rule in (
            (
                "admin",
                test_py,
                f'X = "{ADMIN}"\n',
                1,
                'X = "login-keychain-password"\n',
                "keychain-admin",
            ),
            (
                "admin go",
                test_go,
                go(f'_ = "{SETTINGS}"'),
                6,
                go('_ = "importkey x.keychain-db"'),
                "keychain-admin",
            ),
            (
                "login",
                "tests/a.py",
                f'P = "~/{LOGIN}"\n',
                1,
                'P = "*/Library/Keychains*"\n',
                "login-keychain-path",
            ),
            (
                "framework",
                "tests/x/run.sh",
                f"{sh}cc {FRAMEWORK} a.c\n",
                2,
                f"{sh}echo the Security API\n",
                "security-framework",
            ),
            (
                "keyring import",
                "tests/a.py",
                "import keyring.backends\n",
                1,
                'X = "keyring"\n',
                "credential-library",
            ),
            (
                "keyring from",
                "tests/a.py",
                "from keyring import get_password\n",
                1,
                "# keyring\n",
                "credential-library",
            ),
            (
                "keyring go",
                test_go,
                'package b\n\nimport "github.com/zalando/go-keyring"\n',
                3,
                "package b\n\n// go-keyring\n",
                "credential-library",
            ),
            (
                "exec",
                test_go,
                go('exec.Command("security", "dump")'),
                6,
                go('const security = "/usr/bin/security"'),
                "security-exec",
            ),
            (
                "exec ctx",
                test_go,
                go('exec.CommandContext(ctx, "/usr/bin/security")'),
                6,
                go('exec.Command("securityx")'),
                "security-exec",
            ),
            (
                "exec subcommand",
                test_go,
                go('exec.Command(security, "find-generic-password")'),
                6,
                go('exec.Command(git, "log")'),
                "security-exec",
            ),
            (
                "spawn list",
                test_py,
                'import subprocess\nsubprocess.run(["/usr/bin/security", "dump"])\n',
                2,
                'X = [["/usr/bin/security", "dump"]]\n',
                "security-spawn",
            ),
            (
                "spawn alias",
                test_py,
                'from subprocess import Popen as P\nP(("security",))\n',
                2,
                'from unittest import mock\nmock.patch("subprocess.run", print)\n',
                "security-spawn",
            ),
            (
                "spawn string",
                ".github/a_test.py",
                'import os\nos.system("security dump")\n',
                2,
                'import os\nos.system("securityd")\n',
                "security-spawn",
            ),
            (
                "spawn subcommand",
                test_py,
                'import subprocess\nsubprocess.run([SECURITY, "add-generic-password"])\n',
                2,
                'import subprocess\nsubprocess.run([SECURITY, "add"])\n',
                "security-spawn",
            ),
            (
                "spawn exec",
                test_py,
                'import os\nos.execv("/usr/bin/security", [])\n',
                2,
                'import os\nos.execv("/bin/ls", [])\n',
                "security-spawn",
            ),
            (
                "command sh",
                "tests/x/run.sh",
                f"{sh}x=$(security dump)\n",
                2,
                f"{sh}# security dump\n",
                "security-command",
            ),
            (
                "command shebang",
                "tests/x/run",
                "#!/usr/bin/env zsh\nls | security dump\n",
                2,
                "#!/usr/bin/env python3\nsecurity dump\n",
                "security-command",
            ),
            (
                "command workflow",
                ".github/workflows/w.yml",
                "jobs:\n  a:\n    steps:\n      - run: security dump\n",
                4,
                "jobs:\n  a:\n    name: security dump\n",
                "security-command",
            ),
            (
                "exec ctx call",
                test_go,
                go('exec.CommandContext(t.Context(), "/usr/bin/security", "dump")'),
                6,
                go('exec.Command("grep", "security", "x")'),
                "security-exec",
            ),
            (
                "exec subcommand nested",
                test_go,
                go('exec.Command(bin(), "x", "delete-generic-password")'),
                6,
                go('exec.Command(bin(), "delete")'),
                "security-exec",
            ),
            (
                "spawn keyword",
                test_py,
                'import subprocess\nsubprocess.run(args=["/usr/bin/security", "dump"])\n',
                2,
                'import subprocess\nsubprocess.run(args=["ls"])\n',
                "security-spawn",
            ),
            (
                "spawn mode",
                test_py,
                'import os\nos.spawnl(os.P_WAIT, "/usr/bin/security", "security", "dump")\n',
                2,
                'import os\nos.spawnl(os.P_WAIT, "/bin/ls", "security")\n',
                "security-spawn",
            ),
            (
                "command if",
                "tests/x/run.sh",
                "#!/bin/sh\nif security dump; then :; fi\n",
                2,
                "#!/bin/sh\nif securityd; then :; fi\n",
                "security-command",
            ),
            (
                "command not",
                "tests/x/run.sh",
                "#!/bin/sh\n! security dump\n",
                2,
                "#!/bin/sh\necho no security here\n",
                "security-command",
            ),
            (
                "command quoted run",
                ".github/workflows/w.yml",
                'jobs:\n  a:\n    steps:\n      - run: "security dump"\n',
                4,
                "x:\n  - security tools\n",
                "security-command",
            ),
            (
                "exec ctx commas",
                test_go,
                go('exec.CommandContext(context.WithValue(ctx, k, v), "security", "dump")'),
                6,
                go('exec.CommandContext(context.WithValue(ctx, k, v), "ls")'),
                "security-exec",
            ),
            (
                "exec ctx depth 2",
                test_go,
                go(f'exec.CommandContext({CTX2}, "security", "dump")'),
                6,
                go(f'exec.CommandContext({CTX2}, "ls")'),
                "security-exec",
            ),
            (
                "exec ctx depth 3",
                test_go,
                go(f'exec.CommandContext({CTX3}, "/usr/bin/security")'),
                6,
                go(f'exec.CommandContext({CTX3}, "ls", "security")'),
                "security-exec",
            ),
            (
                "exec subcommand depth 2",
                test_go,
                go(f'exec.CommandContext({CTX2}, bin, "find-generic-password")'),
                6,
                go(f'exec.CommandContext({CTX2}, bin, "log")'),
                "security-exec",
            ),
            (
                "exec subcommand depth 3",
                test_go,
                go(f'exec.CommandContext({CTX3}, bin, "add-internet-password")'),
                6,
                go(f'exec.CommandContext({CTX3}, bin, "log")'),
                "security-exec",
            ),
            (
                "exec paren in string",
                test_go,
                go('exec.CommandContext(f(")", ctx), "security")'),
                6,
                go('exec.CommandContext(f("(", ctx), "ls")'),
                "security-exec",
            ),
            (
                "spawn f-string",
                test_py,
                'import subprocess\nsubprocess.run(f"security {x}", shell=True)\n',
                2,
                'import subprocess\nsubprocess.run(f"securityd {x}", shell=True)\n',
                "security-spawn",
            ),
            (
                "command subshell",
                "tests/x/run.sh",
                "#!/bin/sh\nok && (security dump)\n",
                2,
                "#!/bin/sh\necho 'run this if security fails'\n",
                "security-command",
            ),
            (
                "command prose in workflow",
                ".github/workflows/w.yml",
                "jobs:\n  a:\n    steps:\n      - run: true; if security dump; then :; fi\n",
                4,
                "jobs:\n  a:\n    steps:\n      - name: Fail if security checks fail\n"
                "      - name: Lint (security checks)\n",
                "security-command",
            ),
            (
                "file keychain",
                test_go,
                go("_ = fileKeychain{}"),
                6,
                go("_ = SystemKeychain{}"),
                "file-keychain",
            ),
            ("login call", test_go, go("_ = k.login()"), 6, go("_ = k.Login()"), "file-keychain"),
        ):
            with self.subTest(name=name):
                self.assertEqual(self.found({path: flagged}), [f"{path}:{line}: {rule}"])
                self.assertEqual(self.found({path: missed}), [])

    def test_only_test_files(self):
        text = f'X = "{ADMIN}"\n'
        for path in (
            "src/x.go",
            "src/ensemble/x.py",
            "scripts/x.sh",
            ".github/x.py",
            ".mini-op/runbooks/x/y_test.go",
            "tests/x/fixture.txt",
            "tests/x/README.md",
            "tests/x/data.json",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.found({path: text}), [])
        for path in ("tests/x/data.py", ".github/runner_fixtures.py", "hooks/a.test.tsx"):
            with self.subTest(path=path):
                self.assertEqual(self.found({path: text}), [f"{path}:1: keychain-admin"])

    def test_exceptions_suppress_and_go_stale(self):
        line = "_ = fileKeychain{}"
        path, other = "src/b/a_test.go", "src/b/b_test.go"
        files = {path: go(line), other: go(line)}
        self.assertEqual(
            self.found(files, allowed={key(path, line): "reviewed"}),
            [f"{other}:6: file-keychain"],
        )
        stale = key(path, "_ = fileKeychain{x}")
        self.assertEqual(
            self.found({path: go(line)}, allowed={key(path, line): "reviewed", stale: "gone"}),
            [f"{path}: stale exception {stale[1]}"],
        )
        for reason in ("", "two\nlines"):
            with self.subTest(reason=reason):
                self.assertEqual(
                    self.found({path: go(line)}, allowed={key(path, line): reason}),
                    [f"{path}: exception needs one-line reason"],
                )
