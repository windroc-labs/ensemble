import argparse
import ast
import io
import re
import subprocess
import sys
import tokenize
from collections import namedtuple
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
Rule = namedtuple("Rule", "name pattern limit")
SOURCE = Rule("source", re.compile(""), 10)
TESTS = Rule("tests", re.compile(r"(^|/)tests/|_test\.py$"), 5)
ALLOWANCE = 300
PURPOSE = (
    "The comment budget keeps comments short: meet it by cutting comments or docstrings, "
    "never by adding code to lower the ratio."
)


def chars(text):
    return len("".join(text.split()))


def bare_string(node):
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def shebang(token):
    return token.start[0] == 1 and token.string.startswith("#!")


def measure(text):
    tree = ast.parse(text)
    docstring = sum(
        chars(ast.get_source_segment(text, node)) for node in ast.walk(tree) if bare_string(node)
    )
    comment = sum(
        chars(token.string)
        for token in tokenize.generate_tokens(io.StringIO(text).readline)
        if token.type == tokenize.COMMENT and not shebang(token)
    )
    return comment, docstring, chars(text) - comment - docstring


def code(text):
    """The non-whitespace characters measure counts as code, in order."""
    lines = text.splitlines(keepends=True)
    bare = {
        (s.lineno, len(lines[s.lineno - 1].encode()[: s.col_offset].decode()))
        for s in (node.value for node in ast.walk(ast.parse(text)) if bare_string(node))
    }
    return "".join(
        "".join(token.string.split())
        for token in tokenize.generate_tokens(io.StringIO(text).readline)
        if token.start not in bare and not (token.type == tokenize.COMMENT and not shebang(token))
    )


def rule(path):
    return next(each for each in (TESTS, SOURCE) if each.pattern.search(path))


def git(root, *argv):
    done = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C", str(root)]
        + list(argv),
        capture_output=True,
        text=True,
    )
    if done.returncode:
        raise ValueError(f"git {argv[0]} failed: {done.stderr.strip()}")
    return done.stdout


def changed(root, base):
    """(path, its text at the merge-base or None when new) of each changed .py file."""
    fork = git(root, "merge-base", base, "HEAD").strip()
    listed = git(root, "diff", "--name-only", "--no-renames", "--diff-filter=d", "-z", fork, "--")
    existed = set(git(root, "ls-tree", "-r", "--name-only", "-z", fork).split("\0"))
    for path in sorted(listed.split("\0")):
        if path.endswith(".py") and (root / path).is_file():
            yield path, git(root, "show", f"{fork}:{path}") if path in existed else None


def cut_only(before, after):
    """An edit that keeps the code and adds no comment can only shorten comments."""
    return (
        before is not None
        and code(before) == code(after)
        and sum(measure(after)[:2]) <= sum(measure(before)[:2])
    )


def percent(n, code):
    return f"{n / code:.2%}" if code else "inf" if n else "0.00%"


def main(argv=None, root=ROOT):
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="origin/main")
    args = parser.parse_args(argv)
    root = Path(root)
    files = {SOURCE: [], TESTS: []}
    try:
        for path, base_text in changed(root, args.base):
            try:
                with tokenize.open(root / path) as file:
                    text = file.read()
                comment, docstring, size = measure(text)
                judged = not cut_only(base_text, text)
            except (OSError, SyntaxError, ValueError, tokenize.TokenError) as error:
                raise ValueError(f"{path}: {error}") from error
            if judged:
                files[rule(path)].append((path, comment + docstring, size))
    except ValueError as error:
        print(f"comment_budget: {error}", file=sys.stderr)
        return 2
    failed = False
    for each in (SOURCE, TESTS):
        name = f"{each.name} ({each.limit}% of code + {ALLOWANCE} characters per changed file)"
        over = [
            (path, n, code)
            for path, n, code in files[each]
            if n * 100 > each.limit * code + ALLOWANCE * 100
        ]
        if not over:
            print(f"ok {name}: {len(files[each])} checked")
            continue
        failed = True
        print(f"FAIL {name}: {len(over)} of {len(files[each])} over budget")
        for path, n, code in over:
            allowed = each.limit * code // 100 + ALLOWANCE
            print(
                f"  {path}: {percent(n, code)} ({n} comment+docstring / {code} code characters; "
                f"{allowed} allowed, {n - allowed} over)"
            )
    if failed:
        print(PURPOSE)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
