#!/usr/bin/env python3
"""Check decompose tickets' Scope: ownership against their PRD and target bases.

    scope_check.py --base OWNER/NAME=DIR [--base ...] PRD TICKET...

A ticket's `Repo:` picks its DIR, `Task:` build or light-build makes it an engineer ticket, and
its `Covers:` line names its FR/NFR ids. Its `Scope:` line is followed by `- ` items:
`path` [`[table] key`] [(new)]; other backtick text is prose. A path is a file, a directory or
a glob; it must exist on DIR unless (new). Items of one repo overlap when a path equals or contains
the other's and their keys match or either has none. A PRD backtick literal is a path when it has
no space, starts with none of `/~-$`, holds none of `:<>{}|` and exists on a DIR or holds `/`
before a dotted last segment (a glob without `/` needs a dotted name); it is a key in `[table] key`
form. Each must equal one ticket's item path or key.
"""

import glob
import os
import re
import sys

ENGINEER = {"build", "light-build"}
_ID = re.compile(r"\bN?FR-\d+\b")
_LITERAL = re.compile(r"`([^`\n]+)`")
_KEY = re.compile(r"^\[[A-Za-z][\w.-]*\](?: [\w.-]+)?$")
_GLOB = re.compile(r"[*?[]")


class Usage(Exception):
    pass


def _field(text, name):
    found = re.search(rf"^{name}:[ \t]*(.*)$", text, re.M)
    return found[1].strip() if found else None


def _scope(text):
    lines = text.splitlines()
    if "Scope:" not in (line.strip() for line in lines):
        return None, []
    start = [line.strip() for line in lines].index("Scope:") + 1
    items, bad = [], []
    for line in lines[start:]:
        if not re.match(r"\s*[-*] ", line):
            break
        literals = _LITERAL.findall(line)
        if not literals:
            bad.append(line.strip())
            continue
        key = next((k for k in literals[1:] if _KEY.match(k)), None)
        items.append((literals[0].rstrip("/"), key, "(new)" in line))
    return items, bad


def _ticket(path, text):
    covers = _field(text, "Covers")
    scope, bad = _scope(text)
    return {
        "name": os.path.basename(path),
        "repo": _field(text, "Repo"),
        "task": _field(text, "Task"),
        "covers": set(_ID.findall(covers or "")),
        "scope": scope,
        "bad": bad,
    }


def _expand(tree, path):
    if os.path.isabs(path) or ".." in path.split("/"):
        return []
    if _GLOB.search(path):
        return sorted(os.path.relpath(p, tree) for p in glob.glob(os.path.join(tree, path)))
    return [path] if os.path.exists(os.path.join(tree, path)) else []


def _within(a, b):
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _paths(ticket, bases):
    failures = [f"{ticket['name']}: Scope item `{line}` names no `path`" for line in ticket["bad"]]
    owned = []
    for path, key, new in ticket["scope"] or ():
        found = _expand(bases[ticket["repo"]], path)
        if not found and not new:
            failures.append(f"{ticket['name']}: Scope `{path}` not on {ticket['repo']}'s base")
        owned.extend((p, key) for p in found or [path])
    return failures, owned


def _overlap(left, right):
    for a, key_a in left:
        for b, key_b in right:
            if _within(a, b) and (key_a is None or key_b is None or key_a == key_b):
                return a
    return None


def _prd_literals(prd, trees):
    names = []
    for literal in dict.fromkeys(_LITERAL.findall(prd)):
        if _KEY.match(literal):
            names.append(("key", literal))
            continue
        bare = literal.rstrip("/")
        if (
            not bare
            or re.search(r"\s|[:<>{}|]", bare)
            or bare[0] in "/~-$"
            or ".." in bare.split("/")
        ):
            continue
        last = bare.rsplit("/", 1)[-1]
        if _GLOB.search(bare) and "/" not in bare and "." not in last.strip("."):
            continue
        if any(_expand(tree, bare) for tree in trees) or ("/" in bare and "." in last.strip(".")):
            names.append(("path", bare))
    return names


def _owners(count, noun, names):
    listed = f" ({', '.join(names)})" if names else ""
    return f"owned by {count} {noun}{listed}"


def check(prd: str, tickets: list[dict], bases: dict[str, str]) -> list[str]:
    failures, owned = [], {}
    for ticket in tickets:
        if ticket["task"] in ENGINEER and not ticket["scope"]:
            failures.append(f"{ticket['name']}: no Scope:")
        if ticket["repo"] not in bases:
            failures.append(f"{ticket['name']}: Repo: {ticket['repo']} has no --base")
            continue
        missing, owned[ticket["name"]] = _paths(ticket, bases)
        failures.extend(missing)
    known = [t for t in tickets if t["name"] in owned]
    for i, left in enumerate(known):
        for right in known[i + 1 :]:
            if left["repo"] == right["repo"]:
                shared = _overlap(owned[left["name"]], owned[right["name"]])
                if shared:
                    failures.append(
                        f"{left['name']} and {right['name']}: Scope overlaps on `{shared}`"
                    )
    for kind, literal in _prd_literals(prd, list(bases.values())):
        slot = 0 if kind == "path" else 1
        names = [t["name"] for t in tickets if any(i[slot] == literal for i in t["scope"] or ())]
        if len(names) != 1:
            failures.append(f"PRD names `{literal}`: {_owners(len(names), 'tickets', names)}")
    ids = dict.fromkeys(re.findall(r"^\|\s*(N?FR-\d+)\s*\|", prd, re.M))
    for ident in ids:
        names = [t["name"] for t in tickets if t["task"] in ENGINEER and ident in t["covers"]]
        if len(names) != 1:
            failures.append(f"{ident}: {_owners(len(names), 'engineer tickets', names)}")
    return failures


def _parse(argv):
    bases, files = {}, []
    args = iter(argv)
    for arg in args:
        if arg != "--base":
            files.append(arg)
            continue
        repo, sep, tree = next(args, "").partition("=")
        if not (repo and sep and tree):
            raise Usage("usage: --base OWNER/NAME=DIR")
        if not os.path.isdir(tree):
            raise Usage(f"{tree}: not a directory")
        bases[repo] = tree
    if not bases or not files:
        raise Usage("usage: scope_check.py --base OWNER/NAME=DIR [--base ...] PRD TICKET...")
    return bases, files


def main(argv: list[str], *, out=sys.stdout) -> int:
    try:
        bases, files = _parse(argv)
        texts = []
        for path in files:
            with open(path, encoding="utf-8") as file:
                texts.append(file.read())
    except (Usage, OSError, UnicodeError) as error:
        print(f"check failed: {error}", file=out)
        return 2
    tickets = [_ticket(path, text) for path, text in zip(files[1:], texts[1:], strict=True)]
    failures = check(texts[0], tickets, bases)
    print("\n".join(failures) or "ok", file=out)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
