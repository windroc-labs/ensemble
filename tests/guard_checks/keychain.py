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
    "file-keychain": re.compile(r"fileKeychain\{|\.login\(\)"),
}
# A Go exec call's start (security-exec): its program is its first argument, or the second after a
# context; its arguments, at any nesting depth, are read by _arguments.
EXEC = re.compile(r"exec\.Command(Context)?\(")
GO_PROGRAM = re.compile(r'\s*"(?:/usr/bin/)?security"')
# Go's string, raw string, rune and comment tokens, so their brackets and commas are skipped, then
# the brackets and commas a call's arguments nest and split by.
GO_TOKEN = re.compile(
    r'"(?:[^"\\\n]|\\.)*"|`[^`]*`|\'(?:[^\'\\\n]|\\.)*\'|//[^\n]*|/\*.*?\*/|[()\[\]{},]', re.S
)
# The program at command position: a line's start (or a workflow run:'s) or after a separator, then
# any subshell parens and shell keywords.
COMMAND = re.compile(
    r"(?:^\s*(?:(?:-\s+)?run:\s*[\"']?)?|(?:[;&|`]|\$\()\s*)"
    r"(?:(?:\(|(?:if|then|else|elif|do|while|until|exec|command|time|!)\s)\s*)*"
    r"(?:/usr/bin/)?security\s+[A-Za-z]"
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
        found.update((line, "security-exec") for line in _security_execs(text))
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


def _security_execs(text):
    found = set()
    for call in EXEC.finditer(text):
        args, strings = _arguments(text, call.end())
        program = args[1:2] if call.group(1) else args[:1]
        if any(GO_PROGRAM.match(arg) for arg in program) or any(
            SUBCOMMAND.fullmatch(string) for string in strings
        ):
            found.add(text.count("\n", 0, call.start()) + 1)
    return found


def _arguments(text, start):
    """The top-level arguments of the Go call whose "(" ends at start, and the contents of every
    double-quoted string in them."""
    args, strings, depth = [], [], 0
    for token in GO_TOKEN.finditer(text, start):
        mark = token.group()
        if mark.startswith('"'):
            strings.append(mark[1:-1])
        elif mark in ("(", "[", "{"):
            depth += 1
        elif depth and mark in (")", "]", "}"):
            depth -= 1
        elif not depth and mark in (",", ")"):
            args.append(text[start : token.start()])
            start = token.end()
            if mark == ")":
                break
    return args, strings


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
        elif isinstance(node, ast.Call) and SPAWNS.fullmatch(name := _dotted(node.func, bound)):
            # os.spawn*'s first argument is the mode; a program may also come as args=.
            first = 1 if name.startswith("os.spawn") else 0
            program = node.args[first : first + 1] + [
                k.value for k in node.keywords if k.arg == "args"
            ]
            if any(_calls_security(arg) for arg in program):
                found.add((node.lineno, "security-spawn"))
    return found


def _calls_security(arg):
    if isinstance(arg, ast.JoinedStr):  # an f-string: its leading literal part
        arg = arg.values[0] if arg.values else arg
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return (arg.value.split() or [""])[0] in PROGRAMS
    if not isinstance(arg, ast.List | ast.Tuple):
        return False
    words = [e.value if isinstance(e, ast.Constant) else None for e in arg.elts]
    return (bool(words) and words[0] in PROGRAMS) or any(
        isinstance(word, str) and SUBCOMMAND.fullmatch(word) for word in words
    )
