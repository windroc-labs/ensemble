"""Comments and docstrings cite no internal issue or requirement id: a reader without the board
cannot look one up. Ids in string literals are data (test inputs, help examples) and pass."""

import ast
import io
import os
import re
import tokenize

from tests.guard_checks.common import _files

TOPS = ("src", "tests", ".github", "hooks", "scripts", "skills", "bin", "launchd")
ID = re.compile(r"(?<![\w-])(?:WDRC-\d+|N?FR-\d+|AC\d+|Q\d\d|D\d+)(?![\w-])")
# Where each file kind keeps its comments: a JSON schema's are its $comment values.
MARKERS = {
    **dict.fromkeys((".go", ".js", ".mjs", ".ts", ".tsx"), re.compile(r"(?:^|\s)//(.*)")),
    **dict.fromkeys((".sh", ".toml", ".yml", ".yaml"), re.compile(r"(?:^|\s)#(.*)")),
    ".json": re.compile(r'"\$comment":\s*"((?:[^"\\]|\\.)*)"'),
}


def internal_ids(repo: str) -> list[str]:
    found = []
    for top in TOPS:
        for rel, text in _files(os.path.join(repo, top), (".py", *MARKERS)):
            path = f"{top}/{rel}"
            for line, comment in _comments(path, text):
                found.extend(
                    f"{path}:{line}: {match}: state the rule it stands for instead of an internal id"
                    for match in ID.findall(comment)
                )
    return sorted(found)


def _comments(path, text):
    if path.endswith(".py"):
        yield from _python_comments(text)
        return
    marker = MARKERS[os.path.splitext(path)[1]]
    for number, line in enumerate(text.split("\n"), 1):
        if match := marker.search(line):
            yield number, match.group(1)


def _python_comments(text):
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT:
            yield token.start[0], token.string
    lines = text.split("\n")
    for node in ast.walk(ast.parse(text)):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            for number in range(node.lineno, node.end_lineno + 1):
                yield number, lines[number - 1]
