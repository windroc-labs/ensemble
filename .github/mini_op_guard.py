"""Reject sensitive literals in .mini-op/ and, on a pull request that changes it, in the PR's title,
body and commit messages. Findings name the place and the pattern class, never the matched text.
"""

import ipaddress
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TREE = ".mini-op"
# Public values that may appear anywhere; add an entry only through review.
ALLOWED = frozenset({"noreply@anthropic.com"})
PRIVATE = tuple(
    ipaddress.ip_network(net)
    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")
)
QUAD = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d]|\.\d)")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
LOCAL = re.compile(r"[A-Za-z0-9-]\.local(?![\w-]|\.\w)")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+:-]*")
WORD = r"(?<![\w/.-]){}(?![\w-])"
# Commands whose arguments at these positions are keychain item names.
ITEM_ARGS = {
    "keychain_read": (0, 1),
    "keychain_has": (0, 1),
    "storekey": (0, 1),
    "storekey-stdin": (0, 1),
    "lq": (0, 1),
    "importkey": (1, 2),
}
COMMANDS = re.compile(
    r"(?:^|[\s/;|&(`'\"])("
    + "|".join(re.escape(c) for c in sorted(ITEM_ARGS, key=len, reverse=True))
    + r")(?![\w-])((?:[ \t]+[^\s|;&)]+)*)"
)
SECURITY = re.compile(WORD.format("security") + r"|\w-(?:generic|internet)-password\b")
OPTION = re.compile(r"(?<=\s)-[sal][ \t]+([^\s|;&)]+)")
SHA = re.compile(r"[0-9a-f]{40}")


class GuardError(Exception):
    """A fixed reason the guard cannot decide; it never carries event or git text."""


def _literal(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return NAME.fullmatch(value) is not None


def _items(line):
    for match in COMMANDS.finditer(line):
        args = match[2].split()
        if any(i < len(args) and _literal(args[i]) for i in ITEM_ARGS[match[1]]):
            return True
    return bool(SECURITY.search(line)) and any(_literal(v) for v in OPTION.findall(line))


def classes(line):
    found = []
    if any(
        any(ipaddress.ip_address(quad) in net for net in PRIVATE)
        for quad in QUAD.findall(line)
        if all(int(octet) <= 255 for octet in quad.split("."))
    ):
        found.append("private IPv4")
    if any(match not in ALLOWED for match in EMAIL.findall(line)):
        found.append("email address")
    if UUID.search(line):
        found.append("UUID")
    if LOCAL.search(line):
        found.append(".local host name")
    if _items(line):
        found.append("keychain item literal")
    return found


def scan(label, text):
    return [
        f"{label}:{number}: {kind}"
        for number, line in enumerate(text.splitlines(), 1)
        for kind in classes(line)
    ]


def _entries(root):
    base = root / TREE
    if not base.is_dir() or base.is_symlink():
        return [base] if base.is_symlink() else []
    entries = []
    for top, dirs, files in os.walk(base):
        top = Path(top)
        for name in list(dirs):
            if (top / name).is_symlink():
                dirs.remove(name)
                files.append(name)
        entries.extend(top / name for name in files)
    return sorted(entries, key=lambda path: path.relative_to(root).as_posix())


def scan_tree(root):
    findings, count = [], 0
    for index, path in enumerate(_entries(root), 1):
        rel = path.relative_to(root).as_posix()
        named = [kind for part in rel.split("/") for kind in classes(part)]
        label = f"{TREE}/<entry #{index}>" if named else rel
        own = [f"{label}: {kind}" for kind in dict.fromkeys(named)]
        if path.is_symlink() or not path.is_file():
            own.append(f"{label}: not a regular file")
        else:
            text = _text(path.read_bytes())
            own += [f"{label}: not UTF-8 text"] if text is None else scan(label, text)
            count += 1
        findings += own
    return findings, count


def _text(data):
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _git(root, args, reason):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)
    if result.returncode:
        raise GuardError(reason)
    return result.stdout.decode("utf-8", "replace")


def _event(environ):
    try:
        with open(environ["GITHUB_EVENT_PATH"], encoding="utf-8") as file:
            text = file.read()
    except (KeyError, OSError, UnicodeDecodeError) as error:
        raise GuardError("cannot read the event file") from error
    try:
        pr = json.loads(text)["pull_request"]
        title, body = pr.get("title") or "", pr.get("body") or ""
        base, head = pr["base"]["sha"], pr["head"]["sha"]
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise GuardError("malformed event") from error
    if not all(isinstance(sha, str) and SHA.fullmatch(sha) for sha in (base, head)):
        raise GuardError("bad base or head sha")
    if not isinstance(title, str) or not isinstance(body, str):
        raise GuardError("malformed event")
    return title, body, base, head


def scan_pull_request(root, environ):
    title, body, base, head = _event(environ)
    diff = ["diff", "--name-only", "-z", f"{base}...{head}", "--", f"{TREE}/"]
    if not _git(root, diff, "git diff failed").strip("\0"):
        return [], "PR fields skipped: no .mini-op/ change"
    log = _git(root, ["log", "-z", "--format=%H%x1f%B", f"{base}..{head}"], "git log failed")
    findings = scan("PR title", title) + scan("PR body", body)
    for record in filter(None, log.split("\0")):
        sha, _, message = record.partition("\x1f")
        findings += scan(f"commit {sha.strip()[:12]}", message)
    return findings, "PR fields clean" if not findings else ""


def main(argv=None, *, root=ROOT, environ=os.environ):
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        print("usage: mini_op_guard.py", file=sys.stderr)
        return 2
    root = Path(root)
    findings, count = scan_tree(root)
    note = ""
    if environ.get("GITHUB_EVENT_NAME") == "pull_request":
        try:
            more, note = scan_pull_request(root, environ)
        except GuardError as error:
            print(*findings, sep="\n", end="\n" if findings else "")
            print(f"mini-op guard: {error}")
            return 1
        findings += more
    if findings:
        print(*findings, sep="\n")
        print(f"mini-op guard: {len(findings)} findings")
        return 1
    print(f"mini-op guard: {count} files clean")
    if note:
        print(note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
