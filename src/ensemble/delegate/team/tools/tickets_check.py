#!/usr/bin/env python3
"""Check ticket description files against the PRD they decompose, without changing them.

  tickets_check.py scenario PRD S-<n>...                       print those scenarios as the PRD has them
  tickets_check.py check PRD TICKETS_DIR [--edges FILE] [--drop FR-<n>...]
                                                               check every *.md description in the directory

`check` reads each description's `Repo:`, `Covers:` and `Task:` lines; a description with `Task: verify`
is a verifier ticket. The edges file has one line per blocked ticket: `<its file>: <blocker file> ...`.
Exit 0: all checks hold; 1: the items printed fail; 2: the check could not run.
"""

import argparse
import os
import re
import sys

_ID = re.compile(r"\b(N?FR-\d+)\b")
_TAG = re.compile(r"\s*@(S-\d+)\b(.*)")
_RANGE = re.compile(r"\b(N?FR)-(\d+)\s*(?:–|—|~|\.\.|to|至|到)\s*(?:N?FR-)?(\d+)")


def prd_model(text):
    """Requirement ids from table rows; scenarios (tags, stripped non-empty lines) from gherkin blocks."""
    requirements = list(dict.fromkeys(re.findall(r"^\|\s*(N?FR-\d+)\s*\|", text, re.M)))
    scenarios, current, fenced = {}, None, False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced, current = line.lstrip().startswith("```gherkin"), None
            continue
        if not fenced:
            continue
        tag = _TAG.match(line)
        if tag:
            current = tag.group(1)
            scenarios[current] = {"tags": _ID.findall(tag.group(2)), "lines": [], "raw": []}
        elif line.strip().startswith("Feature:"):
            current = None
        if current:
            scenarios[current]["raw"].append(line)
            if line.strip():
                scenarios[current]["lines"].append(line.strip())
    return requirements, scenarios


def covered(body):
    found = []
    for line in body.splitlines():
        if line.startswith("Covers:"):
            for kind, low, high in _RANGE.findall(line):
                found += [f"{kind}-{n}" for n in range(int(low), int(high) + 1)]
            found += _ID.findall(line)
    return list(dict.fromkeys(found))


def field(body, name):
    match = re.search(rf"^{name}:[ \t]*(.*)$", body, re.M)
    return match.group(1).strip() if match else None


def quotes(scenario, lines):
    want = scenario["lines"]
    return any(lines[at : at + len(want)] == want for at in range(len(lines) - len(want) + 1))


def cycle(edges):
    state = {}

    def visit(node, path):
        if state.get(node) == "done":
            return None
        if state.get(node) == "open":
            return path[path.index(node) :] + [node]
        state[node] = "open"
        for other in edges.get(node, []):
            found = visit(other, path + [node])
            if found:
                return found
        state[node] = "done"
        return None

    for node in edges:
        found = visit(node, [])
        if found:
            return found
    return None


def read_edges(path, bodies, failures):
    edges = {name: [] for name in bodies}
    if not path:
        return edges
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if ":" not in line:
                continue
            name, _, rest = line.partition(":")
            for one in [name.strip(), *rest.split()]:
                if one not in bodies:
                    failures.append(f"edges: {one} is not a description file in the directory")
            if name.strip() in bodies:
                edges[name.strip()] += [b for b in rest.split() if b in bodies]
    return edges


def check(prd, directory, dropped, edges_path, out):
    requirements, scenarios = prd_model(prd)
    names = sorted(n for n in os.listdir(directory) if n.endswith(".md"))
    if not names or not requirements:
        print("check failed: no description files, or no requirement table in the PRD", file=out)
        return 2
    bodies = {}
    for name in names:
        with open(os.path.join(directory, name), encoding="utf-8") as handle:
            bodies[name] = handle.read()
    failures = []
    for name, body in bodies.items():
        missing = [f for f in ("Repo", "Covers", "Task") if not field(body, f)]
        if missing:
            failures.append(f"{name}: no {', '.join(m + ':' for m in missing)} line")
    engineer = {n: b for n, b in bodies.items() if field(b, "Task") != "verify"}
    cover = {n: covered(b) for n, b in engineer.items()}
    in_scope = [r for r in requirements if r not in dropped]
    for requirement in in_scope:
        owners = [n for n, c in cover.items() if requirement in c]
        if len(owners) != 1:
            failures.append(
                f"{requirement}: in {len(owners)} engineer tickets' Covers: ({', '.join(owners) or 'none'}), want 1"
            )
    for name, ids in cover.items():
        unknown = [i for i in ids if i not in requirements]
        if unknown:
            failures.append(
                f"{name}: Covers: names {', '.join(unknown)}, not in the PRD's requirement tables"
            )
    stripped = {
        n: [line.strip() for line in b.splitlines() if line.strip()] for n, b in engineer.items()
    }
    for sid, scenario in scenarios.items():
        if scenario["tags"] and all(t in dropped for t in scenario["tags"]):
            continue
        holders = [n for n, lines in stripped.items() if quotes(scenario, lines)]
        tagged = [
            n
            for n, lines in stripped.items()
            if any(re.match(rf"@{sid}\b", line) for line in lines)
        ]
        if len(holders) != 1:
            altered = sorted(set(tagged) - set(holders))
            note = f"; tagged but not verbatim in {', '.join(altered)}" if altered else ""
            failures.append(
                f"{sid}: quoted verbatim in {len(holders)} engineer tickets ({', '.join(holders) or 'none'}), want 1{note}"
            )
        elif not set(scenario["tags"]) & set(cover[holders[0]]):
            failures.append(
                f"{sid}: quoted in {holders[0]}, whose Covers: has none of {', '.join(scenario['tags'])}"
            )
    edges = read_edges(edges_path, bodies, failures)
    loop = cycle(edges)
    if loop:
        failures.append("dependency cycle: " + " -> ".join(loop))
    for line in failures:
        print(line, file=out)
    print(
        f"{len(names)} descriptions, {len(in_scope)} requirements, {len(scenarios)} scenarios: "
        f"{'ok' if not failures else str(len(failures)) + ' failures'}",
        file=out,
    )
    return 1 if failures else 0


def main(argv, out=sys.stdout):
    parser = argparse.ArgumentParser(prog="tickets_check.py")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("scenario")
    one.add_argument("prd")
    one.add_argument("ids", nargs="+")
    full = sub.add_parser("check")
    full.add_argument("prd")
    full.add_argument("directory")
    full.add_argument("--edges")
    full.add_argument("--drop", nargs="*", default=[])
    try:
        args = parser.parse_args(argv)
        with open(args.prd, encoding="utf-8") as handle:
            prd = handle.read()
        if args.command == "check":
            return check(prd, args.directory, set(args.drop), args.edges, out)
    except (OSError, SystemExit) as error:
        print(f"check failed: {error}", file=out)
        return 2
    _, scenarios = prd_model(prd)
    unknown = [i for i in args.ids if i not in scenarios]
    if unknown:
        print(f"check failed: no scenario {', '.join(unknown)} in the PRD", file=out)
        return 2
    print("\n\n".join("\n".join(scenarios[i]["raw"]).rstrip() for i in args.ids), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
