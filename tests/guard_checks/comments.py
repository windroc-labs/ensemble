"""Comments, docstrings and Python def and class names cite no internal issue or requirement id: a
reader without the board cannot look one up. Ids in string literals are data (test inputs, help
examples) and pass."""

import ast
import io
import os
import re
import tokenize

from tests.guard_checks.common import _files

TOPS = ("src", "tests", ".github", "hooks", "scripts", "skills", "bin", "launchd")
ID = re.compile(r"(?<![\w-])(?:WDRC-\d+|N?FR-\d+|AC\d+|Q\d\d|D\d+)(?![\w-])")
NAME_ID = re.compile(r"(?<![a-z0-9])(?:wdrc|n?fr|ac)_?\d+(?![a-z0-9])")
QUOTED = r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\''
# A line's comment is group 1 of its first match whose group 1 is set; a quoted string before it is
# skipped, so a marker inside one starts no comment. A JSON schema's comments are its $comment values.
MARKERS = {
    **dict.fromkeys(
        (".go", ".js", ".mjs", ".ts", ".tsx"), re.compile(QUOTED + r"|`[^`]*`|(?:^|(?<=\s))//(.*)")
    ),
    **dict.fromkeys((".sh", ".toml", ".yml", ".yaml"), re.compile(QUOTED + r"|(?:^|(?<=\s))#(.*)")),
    ".json": re.compile(r'"\$comment":\s*"((?:[^"\\]|\\.)*)"'),
}


def internal_ids(repo: str) -> list[str]:
    found = []
    for top in TOPS:
        for rel, text in _files(os.path.join(repo, top), (".py", *MARKERS)):
            path = f"{top}/{rel}"
            found.extend(
                f"{path}:{line}: {match}: state the rule it stands for instead of an internal id"
                for line, match in _ids(path, text)
            )
    return sorted(found)


def _ids(path, text):
    if path.endswith(".py"):
        yield from _python_ids(text)
        return
    marker = MARKERS[os.path.splitext(path)[1]]
    for number, line in enumerate(text.split("\n"), 1):
        for found in marker.finditer(line):
            if found.group(1) is not None:
                yield from ((number, match) for match in ID.findall(found.group(1)))
                break


def _python_ids(text):
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT:
            yield from ((token.start[0], match) for match in ID.findall(token.string))
    lines = text.split("\n")
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", node.name).lower()
            yield from ((node.lineno, match) for match in NAME_ID.findall(words))
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            for number in range(node.lineno, node.end_lineno + 1):
                yield from ((number, match) for match in ID.findall(lines[number - 1]))
