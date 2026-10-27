"""No test file calls a real OS credential store: a check that needs a real Keychain is a mini-op
runbook run on the test Mac mini (.mini-op/runbooks/broker-keychain.md), never a test.

Known limits: only test files are read, so a test that reaches a store through production code is
not seen (SystemKeychain refuses in tests through CheckInstalled; integration tests' route to the
installed broker is unguarded); a program named through a variable is caught only when its argv
also holds a literal password subcommand; and no CI job compiles the runbook's Go test, so a
rename in keychain_darwin.go breaks it only on the next mini run. Each regex below is written so
this module matches none of them.
"""

import ast
import hashlib
import os
import re

from tests.guard_checks.common import _bindings, _dotted, _files
from tests.guard_data import KEYCHAIN_FREE_ALLOWED

TOPS = ("tests", ".github", "src", "hooks", "scripts", "skills", "bin")
DATA = (".txt", ".md", ".json")
TEST_NAME = re.compile(r"[^/]*(?:_test\.|\.test\.)[^/]*|[^/]*fixtures\.py")
SKIP = re.compile(r"(?:^|/)(?:node_modules|\.cache)/")
SHELLS = re.compile(r"#![^\n]*\b(?:sh|bash|zsh)\b")
WORD = r"(?<![\w-])(?:{})(?![\w-])"
TEXT = {
    "keychain-admin": re.compile(
        WORD.format(
            r"(?:create|delete|list|default|login|unlock|lock)-keychains?"
            r"|set-keychain-(?:settings|password)"
        )
    ),
    "login-keychain-path": re.compile(r"login\.keychain|Library/Keychains[/]"),
    "security-framework": re.compile(
        r"Security\.framework|-framework[ ]Security|Security/Security\.h"
    ),
}
GO = {
    "credential-library": re.compile(
        r'"[^"\n]*(?:go-keychain|go-keyring|99designs/keyring)[^"\n]*"'
    ),
    "security-exec": re.compile(
        r'exec\.Command(?:Context)?\(\s*(?:[^,()]+,\s*)?"(?:/usr/bin/)?security"'
        r'|exec\.Command(?:Context)?\([^)]*"(?:find|add|delete)-(?:generic|internet)-password"'
    ),
    "file-keychain": re.compile(r"fileKeychain\{|\.login\(\)"),
}
COMMAND = re.compile(
    r"(?:^\s*(?:-\s+)?(?:run:\s*)?|[;&|(`]\s*|\$\(\s*)(?:/usr/bin/)?security\s+[A-Za-z]"
)
PROGRAMS = {"security", "/usr/bin/security"}
SUBCOMMAND = re.compile(r"(?:find|add|delete)-(?:generic|internet)-password")
SPAWNS = re.compile(
    r"subprocess\.(?:run|Popen|call|check_call|check_output|getoutput|getstatusoutput)"
    r"|os\.(?:system|popen|exec\w*|spawn\w*|posix_spawn\w*)"
    r"|asyncio\.create_subprocess_(?:exec|shell)"
)


def keychain_free_tests(repo: str, *, allowed=None) -> list[str]:
    allowed = KEYCHAIN_FREE_ALLOWED if allowed is None else allowed
    found, used = set(), set()
    for path, text in _test_files(repo):
        lines = text.split("\n")
        for line, rule in _violations(path, text):
            key = (path, hashlib.sha256(lines[line - 1].strip().encode()).hexdigest())
            if key in allowed:
                used.add(key)
            else:
                found.add(f"{path}:{line}: {rule}")
    found.update(f"{path}: stale exception {digest}" for path, digest in allowed.keys() - used)
    found.update(
        f"{path}: exception needs one-line reason"
        for (path, _), reason in allowed.items()
        if not isinstance(reason, str) or not reason.strip() or "\n" in reason
    )
    return sorted(found)


def _test_files(repo):
    for top in TOPS:
        for rel, text in _files(os.path.join(repo, top), "", errors="replace"):
            path = f"{top}/{rel}"
            if SKIP.search(path) or path.endswith(DATA):
                continue
            if (
                top == "tests"
                or (path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")))
                or TEST_NAME.fullmatch(path.rsplit("/", 1)[-1])
            ):
                yield path, text


def _violations(path, text):
    found = {(line, rule) for rule, pattern in TEXT.items() for line in _lines(pattern, text)}
    if path.endswith("_test.go"):
        found.update((line, rule) for rule, pattern in GO.items() for line in _lines(pattern, text))
    if path.endswith(".py"):
        found.update(_python(text))
    if (
        path.endswith(".sh")
        or path.startswith(".github/workflows/")
        or (path.startswith("tests/") and SHELLS.match(text))
    ):
        found.update(
            (number, "security-command")
            for number, line in enumerate(text.split("\n"), 1)
            if not line.lstrip().startswith("#") and COMMAND.search(line)
        )
    return found


def _lines(pattern, text):
    return {text.count("\n", 0, match.start()) + 1 for match in pattern.finditer(text)}


def _python(text):
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    bound = _bindings(tree)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module]
            if any(name and name.split(".")[0] == "keyring" for name in names):
                found.add((node.lineno, "credential-library"))
        elif (
            isinstance(node, ast.Call)
            and node.args
            and SPAWNS.fullmatch(_dotted(node.func, bound))
            and _calls_security(node.args[0])
        ):
            found.add((node.lineno, "security-spawn"))
    return found


def _calls_security(arg):
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return (arg.value.split() or [""])[0] in PROGRAMS
    if not isinstance(arg, ast.List | ast.Tuple):
        return False
    words = [e.value for e in arg.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    first = arg.elts[0] if arg.elts else None
    program = isinstance(first, ast.Constant) and first.value in PROGRAMS
    return program or any(SUBCOMMAND.fullmatch(word) for word in words)
