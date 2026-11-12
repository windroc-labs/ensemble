#!/usr/bin/env python3
"""Scan a git checkout for content that must not be public. Standard library only.

Local run, from the repo root, with the private-name list kept outside the checkout:

    python3 scanner/scan.py --names-file <path to your private-name list>

The list may come from the environment variable REPO_GUARDS_PRIVATE_NAMES instead.
It scans the tracked files and the messages and author and committer emails of the
commits in `--commits` (default origin/main..HEAD); `--pr-from-env` adds the PR title
and body from PR_TITLE and PR_BODY.
`--public` prints counts only; the default output gives a position for every hit
but never the matched text. Leak-class hits in files are reported by the file's
1-based index in `git ls-files`, never by its path. Exit 0 clean, 1 hits or list
unavailable, 2 usage error.
"""

import argparse
import html
import os
import posixpath
import re
import stat
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import unquote

NAMES_ENV = "REPO_GUARDS_PRIVATE_NAMES"
LEAK, HYGIENE = "leak", "hygiene"
EM_DASH = chr(0x2014)
UNAVAILABLE = "private-name: 名单不可用 (private-name list unavailable)"
LEAK_NOTICE = 'Leak-class hits are already public: follow "If something leaks" in AGENTS.md.'


class Usage(Exception):
    pass


Finder = Callable[[str], Iterable[int]]


@dataclass(frozen=True)
class Where:
    """One hit's position. `index` is the file's 1-based place in `git ls-files`."""

    path: str = ""
    index: int = 0
    sha: str = ""
    field: str = ""
    line: int = 0
    col: int = 0

    def render(self, leak: bool) -> str:
        if self.index:
            name = "file #%d" % self.index if leak else self.path
            return "%s path" % name if self.field == "path" else "%s:%d:%d" % (name, self.line, self.col)
        if self.sha:
            return "commit %s %s" % (self.sha, self.field or "line %d" % self.line)
        return "%s line %d" % (self.field, self.line)


@dataclass(frozen=True)
class Check:
    name: str
    klass: str
    in_paths: bool


CHECKS = {
    c.name: c
    for c in (
        Check("private-name", LEAK, True),
        Check("local-path", LEAK, True),
        Check("email", LEAK, True),
        Check("em-dash", HYGIENE, False),
        Check("link", HYGIENE, False),
    )
}

# A home directory, then any name segment except exactly <placeholder> (FR-10). The segment
# ends at a separator, whitespace, the line end, closing markup around the path, or
# sentence punctuation followed by whitespace or the line end.
LOCAL_PATH = re.compile(
    r"(?:/(?:Users|home)|[A-Za-z]:[\\/]+(?i:users))[\\/]+"
    r"(?!<[A-Za-z0-9_-]+>(?:[\\/\s`'\")\]]|[.,;:]?$|[.,;:]\s))(?=[^\s/\\])"
)
EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@"
    r"([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})(?![A-Za-z0-9-])"
)
ALLOWED_ADDRESSES = {"noreply@github.com", "noreply@anthropic.com"}
ALLOWED_DOMAINS = ("users.noreply.github.com",)
ALLOWED_PARENTS = ("example.com", "example.org", "example.net")
ALLOWED_SUFFIXES = (".example", ".test")


def email_allowed(address: str) -> bool:
    address = address.lower()
    domain = address.rpartition("@")[2]
    return (
        address in ALLOWED_ADDRESSES
        or domain in ALLOWED_DOMAINS
        or any(domain == p or domain.endswith("." + p) for p in ALLOWED_PARENTS)
        or domain.endswith(ALLOWED_SUFFIXES)
    )


def find_email(line: str) -> Iterable[int]:
    return [m.start() for m in EMAIL.finditer(line) if not email_allowed(m.group(0))]


def find_local_path(line: str) -> Iterable[int]:
    # Any occurrence hits, whatever precedes it: over-reporting is acceptable, a miss is not.
    return [m.start() for m in LOCAL_PATH.finditer(line)]


def find_em_dash(line: str) -> Iterable[int]:
    return [i for i, ch in enumerate(line) if ch == EM_DASH]


def is_word_char(line: str, i: int) -> bool:
    # Wide (CJK) characters do not join words: a name inside Chinese prose still hits.
    if not 0 <= i < len(line):
        return False
    ch = line[i]
    return ch.isalnum() and unicodedata.east_asian_width(ch) not in ("W", "F")


def private_name_finder(entries: list[str]) -> Finder:
    # Lookahead patterns find overlapping matches, so a rejected match never hides the next one.
    patterns = [re.compile("(?=(%s))" % re.escape(e), re.IGNORECASE) for e in entries]

    def find(line: str) -> Iterable[int]:
        cols = set()
        for pattern in patterns:
            for m in pattern.finditer(line):
                if not (is_word_char(line, m.start(1) - 1) or is_word_char(line, m.end(1))):
                    cols.add(m.start(1))
        return sorted(cols)

    return find


def git(top: str, *args: str) -> bytes:
    proc = subprocess.run(["git", "-C", top, *args], capture_output=True)
    if proc.returncode:
        raise Usage("git %s failed: %s" % (args[0], proc.stderr.decode(errors="replace").strip()))
    return proc.stdout


def inside(path: str, top: str) -> bool:
    return os.path.commonpath([path, top]) == top


def load_names(names_file: str | None, top: str) -> list[str] | None:
    """The list's entries, or None when it is unavailable. A list inside `top` is refused."""
    if names_file:
        path = os.path.realpath(names_file)
        if inside(path, top):
            raise Usage("refusing to run: the private-name list is inside the git work tree; keep it outside the checkout")
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            return None
    elif NAMES_ENV in os.environ:
        data = os.environ[NAMES_ENV].encode("utf-8", "surrogateescape")
    else:
        return None
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    return [s for s in (line.strip() for line in text.splitlines()) if s] or None


@dataclass(frozen=True)
class TrackedFile:
    index: int
    path: str
    text: str | None


def tracked_files(top: str) -> list[TrackedFile]:
    files, seen = [], set()
    for record in git(top, "ls-files", "-s", "-z").split(b"\0"):
        if not record:
            continue
        meta, raw = record.split(b"\t", 1)
        if raw in seen:
            continue
        seen.add(raw)
        path = os.fsdecode(raw)
        text = None if meta.split()[0] == b"160000" else read_text(os.path.join(top, path))
        files.append(TrackedFile(len(files) + 1, path, text))
    return files


def read_text(full: str) -> str | None:
    """A file's UTF-8 text, a symlink's target string (never followed), or None."""
    try:
        mode = os.lstat(full).st_mode
        if stat.S_ISLNK(mode):
            return os.readlink(full)
        if not stat.S_ISREG(mode):
            return None
        with open(full, "rb") as fh:
            return fh.read().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


class Report:
    def __init__(self, checks: list[Check]):
        self.checks = checks
        self.hits: list[tuple[Check, Where]] = []
        self.unavailable = False
        self.skipped = 0

    def add(self, check: Check, where: Where) -> None:
        self.hits.append((check, where))

    def failed(self) -> bool:
        return bool(self.hits) or self.unavailable

    def render(self, public: bool) -> str:
        lines = [] if public else [
            "%s: %s (%s)" % (where.render(check.klass == LEAK), check.name, check.klass)
            for check, where in self.hits
        ]
        if self.unavailable:
            lines.append(UNAVAILABLE)
        lines.append("summary:")
        width = max(len(c.name) for c in self.checks)
        for check in self.checks:
            count = sum(1 for c, _ in self.hits if c is check)
            shown = "unavailable" if check.name == "private-name" and self.unavailable else str(count)
            lines.append("  %s  %-7s  %s" % (check.name.ljust(width), check.klass, shown))
        files = {where.index for _, where in self.hits if where.index}
        lines.append("skipped non-text files: %d" % self.skipped)
        lines.append("files with hits: %d, total hits: %d" % (len(files), len(self.hits)))
        if any(check.klass == LEAK for check, _ in self.hits):
            lines.append(LEAK_NOTICE)
        return "\n".join(lines) + "\n"


def scan_text(report: Report, finders: dict[Check, Finder], text: str, where: Where) -> None:
    for number, line in enumerate(text.split("\n"), 1):
        for check, find in finders.items():
            for col in find(line):
                report.add(check, Where(where.path, where.index, where.sha, where.field, number, col + 1))


def scan_files(report: Report, finders: dict[Check, Finder], files: list[TrackedFile]) -> None:
    path_finders = {c: f for c, f in finders.items() if c.in_paths}
    for f in files:
        for check, find in path_finders.items():
            if find(f.path):
                report.add(check, Where(f.path, f.index, field="path"))
        if f.text is None:
            report.skipped += 1
        else:
            scan_text(report, finders, f.text, Where(f.path, f.index))


@dataclass(frozen=True)
class Commit:
    sha: str
    author: str
    committer: str
    message: str


def commits(top: str, revisions: str) -> list[Commit]:
    out = git(top, "log", "-z", "--format=%H%x00%ae%x00%ce%x00%B", "--end-of-options", revisions)
    fields = out.decode("utf-8", "replace").split("\0")
    if fields and not fields[-1]:
        fields.pop()
    return [Commit(*fields[i : i + 4]) for i in range(0, len(fields) - 3, 4)]


def scan_commits(report: Report, finders: dict[Check, Finder], log: list[Commit]) -> None:
    email = next((c for c in finders if c.name == "email"), None)
    for c in log:
        scan_text(report, finders, c.message, Where(sha=c.sha))
        for field, address in (("author", c.author), ("committer", c.committer)):
            if email and not email_allowed(address):
                report.add(email, Where(sha=c.sha, field=field))


def scan_pull_request(report: Report, finders: dict[Check, Finder]) -> None:
    # Title and body arrive through the environment, never through argv or shell text.
    for field, var in (("title", "PR_TITLE"), ("body", "PR_BODY")):
        scan_text(report, finders, os.environ.get(var, ""), Where(field=field))


FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
CODE_SPAN = re.compile(r"(`+)(?!`).*?(?<!`)\1(?!`)")
INLINE_LINK = re.compile(
    r"!?\[(?:[^\[\]]|\[[^\[\]]*\])*\]\(\s*(<[^>]*>|[^\s()]*(?:\([^\s()]*\)[^\s()]*)*)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)"
)
REFERENCE = re.compile(r"^ {0,3}\[(?!\^)[^\]]+\]:\s*(<[^>]*>|\S+)")
ATX = re.compile(r"^ {0,3}#{1,6}(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
SETEXT = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def outside_fences(text: str) -> Iterable[tuple[int, str]]:
    fence = ""
    for number, line in enumerate(text.split("\n"), 1):
        m = FENCE.match(line)
        if fence:
            closing = m and not line[m.end():].strip()
            if closing and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = ""
        elif m:
            fence = m.group(1)
        else:
            yield number, line


def rendered(markdown: str) -> str:
    """Inline Markdown outside code spans as GitHub renders it to text."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", markdown)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"(?<!\w)(_{1,3})(?![_\s])(.+?)(?<![_\s])\1(?!\w)", r"\2", text)
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: rendered text, lowercased, punctuation and symbols dropped."""
    parts, pos = [], 0
    for m in CODE_SPAN.finditer(heading):
        parts += [rendered(heading[pos : m.start()]), m.group(0).strip("`")]
        pos = m.end()
    text = "".join(parts) + rendered(heading[pos:])
    return "".join(
        "-" if ch == " " else ch
        for ch in text.lower()
        if ch in " -_" or unicodedata.category(ch)[0] in "LMN"
    )


def anchors(text: str) -> set[str]:
    found: dict[str, int] = {}
    previous = ""
    for _, line in outside_fences(text):
        atx = ATX.match(line)
        heading = atx.group(1) or "" if atx else None
        if heading is None and previous.strip() and SETEXT.match(line):
            heading = previous.strip()
        previous = "" if heading is not None else line
        if heading is None:
            continue
        base = name = slug(heading.strip())
        while name in found:
            found[base] += 1
            name = "%s-%d" % (base, found[base])
        found[name] = 0
    return set(found)


def link_targets(text: str) -> Iterable[tuple[int, int, str]]:
    for number, line in outside_fences(text):
        line = CODE_SPAN.sub(lambda m: " " * len(m.group(0)), line)
        ref = REFERENCE.match(line)
        matches = [ref] if ref else INLINE_LINK.finditer(line)
        for m in matches:
            yield number, m.start(1) if ref else m.start(), m.group(1).strip("<>")


class LinkIndex:
    def __init__(self, files: list[TrackedFile]):
        self.files = {f.path: f for f in files}
        self.dirs = {posixpath.dirname(p) for p in self.files}
        self.dirs |= {d for p in list(self.dirs) for d in parents(p)}
        self.anchors: dict[str, set[str]] = {}

    def exists(self, path: str) -> bool:
        return path in self.files or path in self.dirs or path == "."

    def anchors_of(self, path: str) -> set[str] | None:
        f = self.files.get(path)
        if f is None or f.text is None or not path.lower().endswith(".md"):
            return None
        if path not in self.anchors:
            self.anchors[path] = anchors(f.text)
        return self.anchors[path]

    def broken(self, source: str, target: str) -> bool:
        if not target or SCHEME.match(target) or target.startswith("//"):
            return False
        path, _, fragment = target.partition("#")
        resolved = source
        if path:
            base = "" if path.startswith("/") else posixpath.dirname(source)
            resolved = posixpath.normpath(posixpath.join(base, unquote(path).lstrip("/")))
            if resolved == ".." or resolved.startswith("../") or not self.exists(resolved):
                return True
        known = self.anchors_of(resolved) if fragment else None
        return known is not None and unquote(fragment) not in known


def parents(path: str) -> Iterable[str]:
    while path:
        path = posixpath.dirname(path)
        yield path


def scan_links(report: Report, check: Check, files: list[TrackedFile]) -> None:
    index = LinkIndex(files)
    for f in files:
        if f.text is None or not f.path.lower().endswith(".md"):
            continue
        for number, col, target in link_targets(f.text):
            if index.broken(f.path, target):
                report.add(check, Where(f.path, f.index, line=number, col=col + 1))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=".", help="git checkout to scan (default: current directory)")
    parser.add_argument("--checks", default=",".join(CHECKS), help="comma-separated checks (default: all): %(default)s")
    parser.add_argument("--names-file", help="private-name list, one literal per line; must be outside the checkout")
    parser.add_argument("--commits", default="origin/main..HEAD",
                        help="revision range whose commit messages and emails are scanned (default: %(default)s)")
    parser.add_argument("--pr-from-env", action="store_true",
                        help="also scan the PR title and body from the PR_TITLE and PR_BODY variables")
    parser.add_argument("--public", action="store_true", help="print counts only, for public CI logs")
    return parser.parse_args(argv)


def run(argv: list[str]) -> tuple[int, str]:
    args = parse_args(argv)
    names = [n.strip() for n in args.checks.split(",") if n.strip()]
    unknown = [n for n in names if n not in CHECKS]
    if unknown or not names:
        raise Usage("unknown check %r; choose from %s" % (",".join(unknown), ",".join(CHECKS)))
    checks = [c for c in CHECKS.values() if c.name in names]
    top = os.path.realpath(git(os.path.abspath(args.repo), "rev-parse", "--show-toplevel").decode().strip())

    report = Report(checks)
    finders: dict[Check, Finder] = {}
    for check in checks:
        if check.name == "link":
            continue
        if check.name == "private-name":
            entries = load_names(args.names_file, top)
            if entries is None:
                report.unavailable = True
            else:
                finders[check] = private_name_finder(entries)
        else:
            finders[check] = TEXT_FINDERS[check.name]

    log = commits(top, args.commits) if finders else []
    files = tracked_files(top) if finders or "link" in names else []
    scan_files(report, finders, files)
    if "link" in names:
        scan_links(report, CHECKS["link"], files)
    scan_commits(report, finders, log)
    if args.pr_from_env and finders:
        scan_pull_request(report, finders)
    return (1 if report.failed() else 0), report.render(args.public)


TEXT_FINDERS: dict[str, Finder] = {
    "local-path": find_local_path,
    "email": find_email,
    "em-dash": find_em_dash,
}


def main() -> int:
    try:
        code, output = run(sys.argv[1:])
    except Usage as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    sys.stdout.write(output)
    return code


if __name__ == "__main__":
    sys.exit(main())
