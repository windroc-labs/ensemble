"""Fail a PR whose added and changed src lines have under THRESHOLD % unit coverage (lines and
branches combined).

Usage: coverage_gate.py <data file from RUN_TESTS_COVERAGE> <base ref>
"""

import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from run_tests import src_trees

from tests.guard_checks.suppressions import NO_COVER_PRAGMA

THRESHOLD = 85
STUB = r"^\s*(((async )?def .*?)?\)(\s*->.*?)?:\s*)?\.\.\.\s*(#|$)"
TYPE_CHECKING = r"^\s*(el)?if (typing\.)?TYPE_CHECKING:"


def changed_lines(diff_text, trees):
    """New-side line numbers per src `.py` file of a `git diff -U0`."""
    changed, path = {}, None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            path = None
        elif line.startswith("+++ ") and path is None:
            name = line[4:].removeprefix("b/")
            path = (
                name
                if name.endswith(".py") and any(name.startswith(f"{tree}/") for tree in trees)
                else ""
            )
        elif line.startswith("@@ ") and path:
            start, _, count = line.split()[2][1:].partition(",")
            lines = range(int(start), int(start) + int(count or 1))
            if lines:
                changed.setdefault(path, set()).update(lines)
    return changed


def verdict(report, changed):
    """(exit code, output lines) for a coverage JSON report's files and the changed lines."""
    covered = total = 0
    misses = []
    for path in sorted(changed):
        file = report[path]
        lines = {file.get("multiline", {}).get(line, line) for line in changed[path]}
        statements = lines & {*file["executed_lines"], *file["missing_lines"]}
        statements -= set(file["excluded_lines"])
        missing = sorted(lines & set(file["missing_lines"]))
        partial = [arc for arc in file["missing_branches"] if arc[0] in lines]
        hit = [arc for arc in file["executed_branches"] if arc[0] in lines]
        covered += len(statements) - len(missing) + len(hit)
        total += len(statements) + len(hit) + len(partial)
        parts = [f"lines {_ranges(missing)}"] if missing else []
        if partial:
            arcs = (f"{start}->{end if end > 0 else 'exit'}" for start, end in partial)
            parts.append(f"branches {', '.join(arcs)}")
        if parts:
            misses.append(f"{path}: {'; '.join(parts)}")
    if not total:
        return 0, ["no changed src lines"]
    percent = math.floor(1000 * covered / total) / 10
    count = sum(map(len, changed.values()))
    head = f"diff coverage: {percent}% ({covered}/{total}) on {count} changed src lines; threshold {THRESHOLD}%"
    if covered * 100 >= THRESHOLD * total:
        return 0, [head]
    return 1, [head, *misses]


def _ranges(numbers):
    runs = []
    for number in numbers:
        if runs and number == runs[-1][1] + 1:
            runs[-1][1] = number
        else:
            runs.append([number, number])
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def load_report(data_file, paths, root):
    """The JSON report of `paths` (relative to `root`), keyed by those paths, each file's
    `multiline` mapping a multi-line statement's other lines to its first; only `no cover`
    pragmas, `...` stubs and `if TYPE_CHECKING:` exclude code.
    """
    import coverage
    from coverage.parser import PythonParser

    cov = coverage.Coverage(data_file=data_file, config_file=False)
    cov.clear_exclude("exclude")
    cov.clear_exclude("partial")
    for regex in (NO_COVER_PRAGMA, STUB, TYPE_CHECKING):
        cov.exclude(regex)
    cov.load()
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "report.json")
        cov.json_report(morfs=[str(Path(root, path)) for path in paths], outfile=out)
        with open(out, encoding="utf-8") as file:
            files = json.load(file)["files"]
    report = {os.path.relpath(os.path.abspath(name), root): file for name, file in files.items()}
    for path, file in report.items():
        parser = PythonParser(filename=str(Path(root, path)))
        parser.parse_source()
        file["multiline"] = {
            line: first
            for line in range(1, parser.text.count("\n") + 2)
            if (first := parser.first_line(line)) != line
        }
    return report


def main(argv):
    if len(argv) != 2:
        sys.exit("usage: coverage_gate.py <data file> <base ref>")
    data_file, base = argv
    repo = Path(__file__).resolve().parents[1]
    git = ["git", "-C", str(repo)]
    merge_base = subprocess.run(
        [*git, "merge-base", base, "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    diff = subprocess.run(
        [*git, "diff", "-U0", "--no-color", "--no-ext-diff", merge_base, "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    trees = [str(tree.relative_to(repo)) for tree in src_trees(repo)]
    changed = changed_lines(diff, trees)
    report = load_report(data_file, sorted(changed), repo) if changed else {}
    code, lines = verdict(report, changed)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
