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
    "storekey-stdin": (0, 1),
    "lq": (0, 1),
    "importkey": (1, 2),
}
# A command name anywhere, then its words; an item name holds a digit or one of -._@:+, so prose
# after a command name (plain words) passes. A name of letters only is left to FR-212's search.
COMMANDS = re.compile(
    r"(?<![\w-])("
    + "|".join(re.escape(c) for c in sorted(ITEM_ARGS, key=len, reverse=True))
    + r")(?![\w-])(?=((?:[ \t]+[^\s|;&)`<>]+)*))"
)
SECURITY = re.compile(WORD.format("security") + r"|\w-(?:generic|internet)-password\b")
OPTION = re.compile(r"(?<=\s)-[sal][ \t]+([^\s|;&)`<>]+)")
SHA = re.compile(r"[0-9a-f]{40}")


class GuardError(Exception):
    """A fixed reason the guard cannot decide; it never carries event or git text."""


def _literal(value):
    """A keychain item name written out, not a variable, placeholder or format."""
    return NAME.fullmatch(value.strip("'\"`")) is not None


def _named(value):
    """A literal that cannot be a plain prose word or another command's name."""
    value = value.strip("'\"`.,")
    return (
        _literal(value) and value not in ITEM_ARGS and re.search(r"[\d._@:+-]", value) is not None
    )


def _items(line):
    """Whether the line passes a literal item name to a keychain reader or writer."""
    for match in COMMANDS.finditer(line):
        args = match[2].split()
        if any(i < len(args) and _named(args[i]) for i in ITEM_ARGS[match[1]]):
            return True
    return bool(SECURITY.search(line)) and any(_literal(v) for v in OPTION.findall(line))


def _private(quad):
    octets = [int(octet) for octet in quad.split(".")]
    if any(octet > 255 for octet in octets):
        return False
    address = ipaddress.IPv4Address(bytes(octets))
    return any(address in net for net in PRIVATE)


def classes(line):
    found = []
    if any(_private(quad) for quad in QUAD.findall(line)):
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


def _logical(text):
    """Each line with its number, a backslash-continued line joined to the next."""
    pending, start = "", 0
    for number, line in enumerate(text.splitlines(), 1):
        start = start or number
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        yield start, pending + line
        pending, start = "", 0
    if pending:
        yield start, pending


def scan(label, text):
    return [
        f"{label}:{number}: {kind}" for number, line in _logical(text) for kind in classes(line)
    ]


def _entries(root):
    base = root / TREE
    if base.is_symlink() or (base.exists() and not base.is_dir()):
        return [base]
    if not base.exists():
        return []
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
        if path == root / TREE:
            own.append(f"{label}: not a directory")
        elif path.is_symlink() or not path.is_file():
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
        return [], True
    log = _git(root, ["log", "-z", "--format=%H%x1f%B", f"{base}..{head}"], "git log failed")
    findings = scan("PR title", title) + scan("PR body", body)
    for record in filter(None, log.split("\0")):
        sha, _, message = record.partition("\x1f")
        findings += scan(f"commit {sha.strip()[:12]}", message)
    return findings, False


def main(argv=None, *, root=ROOT, environ=os.environ):
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        print("usage: mini_op_guard.py", file=sys.stderr)
        return 2
    root = Path(root)
    findings, count = scan_tree(root)
    error = skipped = None
    if environ.get("GITHUB_EVENT_NAME") == "pull_request":
        try:
            more, skipped = scan_pull_request(root, environ)
            findings += more
        except GuardError as failure:
            error = failure
    for finding in findings:
        print(finding)
    if error or findings:
        print(f"mini-op guard: {error or f'{len(findings)} findings'}")
        return 1
    print(f"mini-op guard: {count} files clean")
    if skipped is not None:
        print("PR fields skipped: no .mini-op/ change" if skipped else "PR fields clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
