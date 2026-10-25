"""Lint suppression inventory check."""

import ast
import bisect
import io
import re
import tokenize

from tests.guard_checks.common import _tree_files
from tests.guard_data import NO_COVER_ALLOWED, NOQA_ALLOWED

NO_COVER_PRAGMA = r"#\s*(pragma|PRAGMA)[:\s]?\s*(no|NO)\s*(cover|COVER)"


def source_suppressions(repo: str) -> list[str]:
    """Suppressions in every code tree need an exact inventory entry, reason and one use."""
    found, seen = [], set()
    for path, text in _tree_files(repo, ".py"):
        functions = list(_suppression_functions(ast.parse(text)))
        disabled = {}
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type != tokenize.COMMENT or not re.search(
                r"#\s*(?:(?:ruff|flake8)\s*:\s*noqa\b|noqa\b|type\s*:\s*ignore\b|(?:pyright|pylint|isort)\s*:)",
                token.string,
                re.I,
            ):
                continue
            at = f"{path}:{token.start[0]}"
            scope = next(
                (
                    name
                    for first, last, name, _ in reversed(functions)
                    if first <= token.start[0] <= last
                ),
                "<module>",
            )
            pylint = re.fullmatch(
                r"# pylint: (disable|enable)=duplicate-code(?:\s+#\s*(.+?))?\s*",
                token.string,
            )
            if pylint:
                key = (path, "duplicate-code", scope)
                detail = f"suppression duplicate-code in {scope}"
                if pylint[1] == "enable":
                    if key not in disabled or disabled[key] != token.start[1] or pylint[2]:
                        found.append(f"{at}: {detail} has no matching disable")
                    else:
                        del disabled[key]
                elif key not in NOQA_ALLOWED:
                    found.append(f"{at}: {detail} is not allowed")
                elif pylint[2] != NOQA_ALLOWED[key]:
                    found.append(f"{at}: {detail} reason differs from NOQA_ALLOWED")
                elif key in seen:
                    found.append(f"{at}: duplicate {detail}")
                else:
                    disabled[key] = token.start[1]
                    seen.add(key)
                continue
            match = re.fullmatch(
                r"#\s*noqa\s*:\s*([A-Z]+\d+(?:\s*,\s*[A-Z]+\d+)*)\s*#\s*(.+?)\s*",
                token.string,
                re.I,
            )
            if not match:
                found.append(f"{at}: suppression needs explicit codes and a recorded reason")
                continue
            for code in re.split(r"\s*,\s*", match[1].upper()):
                key = (path, code, scope)
                detail = f"suppression {code} in {scope}"
                if key not in NOQA_ALLOWED:
                    found.append(f"{at}: {detail} is not allowed")
                elif match[2] != NOQA_ALLOWED[key]:
                    found.append(f"{at}: {detail} reason differs from NOQA_ALLOWED")
                elif key in seen:
                    found.append(f"{at}: duplicate {detail}")
                else:
                    seen.add(key)
        found.extend(
            f"{path}: suppression {code} in {scope} has no matching enable"
            for _, code, scope in disabled
        )
    found.extend(
        f"{path}: suppression {code} in {scope} has no matching comment"
        for path, code, scope in sorted(NOQA_ALLOWED.keys() - seen)
    )
    return found


def coverage_pragmas(repo: str) -> list[str]:
    """Each coverage exclusion sits on a NO_COVER_ALLOWED function's header (`def` through its
    colon), once, with a reason; each entry has its comment.
    """
    found, seen = [], set()
    for path, text in _tree_files(repo, ".py"):
        lines = [
            i for i, line in enumerate(text.splitlines(), 1) if re.search(NO_COVER_PRAGMA, line)
        ]
        if not lines:
            continue
        headers = _function_headers(text)
        for line in lines:
            scope = next((name for first, last, name in headers if first <= line <= last), None)
            at = f"{path}:{line}"
            if scope is None:
                found.append(f"{at}: pragma no cover must be on a def line")
            elif (path, scope) not in NO_COVER_ALLOWED:
                found.append(f"{at}: pragma no cover in {scope} is not allowed")
            elif (path, scope) in seen:
                found.append(f"{at}: duplicate pragma no cover in {scope}")
            else:
                seen.add((path, scope))
                if not NO_COVER_ALLOWED[path, scope].strip():
                    found.append(f"{at}: pragma no cover in {scope} needs a reason")
    found.extend(
        f"{path}: pragma no cover in {scope} has no matching comment"
        for path, scope in sorted(NO_COVER_ALLOWED.keys() - seen)
    )
    return found


def _function_headers(text):
    colons = [
        token.start
        for token in tokenize.generate_tokens(io.StringIO(text).readline)
        if token.type == tokenize.OP and token.string == ":"
    ]
    headers = []
    for first, _, name, node in _suppression_functions(ast.parse(text)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            body = (node.body[0].lineno, node.body[0].col_offset)
            headers.append((first, colons[bisect.bisect_left(colons, body) - 1][0], name))
    return headers


def _suppression_functions(tree, parents=()):
    for node in ast.iter_child_nodes(tree):
        scope = parents
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = (*parents, node.name)
            if not isinstance(node, ast.ClassDef):
                yield node.lineno, node.end_lineno, ".".join(scope), node
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
            and len(node.test.ops) == 1
            and isinstance(node.test.ops[0], ast.Eq)
            and len(node.test.comparators) == 1
            and isinstance(node.test.comparators[0], ast.Constant)
            and node.test.comparators[0].value == "__main__"
        ):
            scope = (*parents, "__main__")
            yield node.lineno, node.end_lineno, ".".join(scope), node
        yield from _suppression_functions(node, scope)
