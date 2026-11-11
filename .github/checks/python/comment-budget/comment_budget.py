import ast
import io
import subprocess
import sys
import tokenize
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG = ".github/checks/python/comment-budget.toml"
KEYS = ("source_percent", "test_percent", "allowance_chars", "test_paths")
PURPOSE = (
    "The comment budget keeps comments short: meet it by cutting comments or docstrings, "
    "never by adding code to lower the ratio."
)
USAGE = "usage: comment_budget.py [--base REF] [--config PATH] [--all]"


class Error(Exception):
    pass


@dataclass
class Counts:
    comment: int = 0
    code: int = 0

    def __add__(self, other):
        return Counts(self.comment + other.comment, self.code + other.code)


@dataclass
class Measure:
    source: Counts = field(default_factory=Counts)
    tests: Counts = field(default_factory=Counts)


def chars(text):
    return len("".join(text.split()))


def bare_string(node):
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def measure(text, whole_file_is_test):
    try:
        tree = ast.parse(text)
        comments = [
            token.string
            for token in tokenize.generate_tokens(io.StringIO(text).readline)
            if token.type == tokenize.COMMENT
            and not (token.start[0] == 1 and token.string.startswith("#!"))
        ]
    except (SyntaxError, ValueError, tokenize.TokenError) as error:
        raise Error(error) from error
    strings = [ast.get_source_segment(text, node) for node in ast.walk(tree) if bare_string(node)]
    comment = sum(map(chars, comments + strings))
    counts = Counts(comment, chars(text) - comment)
    return Measure(tests=counts) if whole_file_is_test else Measure(source=counts)


def glob(pattern, path):
    def segments(pattern, path):
        if not pattern:
            return not path
        if pattern[0] == "**":
            return any(segments(pattern[1:], path[k:]) for k in range(len(path) + 1))
        return bool(path) and segment(pattern[0], path[0]) and segments(pattern[1:], path[1:])

    def segment(pattern, s):
        head, star, tail = pattern.partition("*")
        if not star:
            return pattern == s
        if not s.startswith(head):
            return False
        rest = s[len(head) :]
        return any(segment(tail, rest[k:]) for k in range(len(rest) + 1))

    return segments(pattern.split("/"), path.split("/"))


@dataclass
class Config:
    source_percent: int
    test_percent: int
    allowance_chars: int
    test_paths: list

    def is_test_path(self, path):
        return any(glob(p, path) for p in self.test_paths)


def parse_config(text):
    try:
        values = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise Error(error) from error
    for key in values:
        if key not in KEYS:
            raise Error(f"unknown key `{key}`")
    for key in KEYS:
        if key not in values:
            raise Error(f"missing key `{key}`")
    for key in KEYS[:3]:
        n = values[key]
        if type(n) is not int or n < 0:
            raise Error(f"`{key}` must be an integer")
    paths = values["test_paths"]
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise Error("`test_paths` must be a list of strings")
    return Config(**values)


@dataclass
class Options:
    base: str = "origin/main"
    config: str | None = None
    all: bool = False


def parse(args):
    options = Options()
    args = iter(args)
    for flag in args:
        if flag == "--all":
            options.all = True
            continue
        if flag not in ("--base", "--config"):
            raise Error(f"unknown argument {flag}\n{USAGE}")
        value = next(args, None)
        if value is None:
            raise Error(f"{flag} needs a value")
        setattr(options, flag[2:], value)
    return options


def git(root, *args):
    done = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C", str(root)]
        + list(args),
        check=False,
        capture_output=True,
        text=True,
    )
    if done.returncode:
        raise Error(f"git {args[0]} failed: {done.stderr.strip()}")
    return done.stdout


def files(root, options):
    if options.all:
        listed = git(root, "ls-files", "-z", "--", "*.py")
    else:
        fork = git(root, "merge-base", options.base, "HEAD").strip()
        listed = git(
            root, "diff", "--name-only", "--no-renames", "--diff-filter=d", "-z", fork, "--"
        )
    return sorted(p for p in listed.split("\0") if p.endswith(".py") and (root / p).is_file())


def percent(n, code):
    return f"{n * 100 / code:.2f}%" if code else "inf" if n else "0.00%"


def run(args, cwd, out):
    options = parse(args)
    cwd = Path(cwd)
    root = Path(git(cwd, "rev-parse", "--show-toplevel").strip())
    path = cwd / options.config if options.config else root / CONFIG
    try:
        config = parse_config(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, Error) as error:
        raise Error(f"config {path}: {error}") from error
    measured = []
    for p in files(root, options):
        try:
            with tokenize.open(root / p) as file:
                text = file.read()
            measured.append((p, measure(text, config.is_test_path(p))))
        except (OSError, SyntaxError, UnicodeDecodeError, Error) as error:
            raise Error(f"{p}: {error}") from error
    allowance = config.allowance_chars

    def limit(m):
        return config.source_percent * m.source.code + config.test_percent * m.tests.code

    over = [
        (p, m)
        for p, m in measured
        if (m.source.comment + m.tests.comment) * 100 > limit(m) + allowance * 100
    ]
    scope = "file" if options.all else "changed file"
    name = (
        f"comment budget (source {config.source_percent}%, tests {config.test_percent}% "
        f"of code + {allowance} characters per {scope})"
    )
    if not over:
        print(f"ok {name}: {len(measured)} checked", file=out)
    else:
        print(f"FAIL {name}: {len(over)} of {len(measured)} over budget", file=out)
        for p, m in over:
            n, code = m.source.comment + m.tests.comment, m.source.code + m.tests.code
            allowed = limit(m) // 100 + allowance
            print(
                f"  {p}: {percent(n, code)} ({n} comment / {code} code characters, "
                f"{m.tests.code} in tests; {allowed} allowed, {n - allowed} over)",
                file=out,
            )
    total = Measure()
    for _, m in measured:
        total = Measure(total.source + m.source, total.tests + m.tests)
    for label, c in (("source", total.source), ("tests", total.tests)):
        print(
            f"total {label}: {c.comment} comment / {c.code} code characters "
            f"({percent(c.comment, c.code)})",
            file=out,
        )
    if over:
        print(PURPOSE, file=out)
    return not over


def main():
    try:
        return 0 if run(sys.argv[1:], Path.cwd(), sys.stdout) else 1
    except Error as error:
        print(f"comment-budget: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
