""".github/coverage_gate.py: changed lines, the 85% verdict and the coverage report it reads."""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coverage_gate.py")
spec = importlib.util.spec_from_file_location("coverage_gate", PATH)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

TREES = ("src",)
NEW = "diff --git a/{0} b/{0}\nnew file mode 100644\n--- /dev/null\n+++ b/{0}\n"


def diff(path, *hunks):
    head = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
    return head + "".join(f"@@ {hunk} @@\n" for hunk in hunks)


def entry(executed=(), missing=(), branches=(), partial=(), excluded=()):
    return {
        "executed_lines": list(executed),
        "missing_lines": list(missing),
        "excluded_lines": list(excluded),
        "executed_branches": [list(arc) for arc in branches],
        "missing_branches": [list(arc) for arc in partial],
    }


class ChangedLines(unittest.TestCase):
    def test_new_side_lines_of_src_python_files(self):
        text = (
            diff("src/ensemble/utils/a.py", "-3,0 +4,2", "-10 +12", "-20,2 +23,0")
            + NEW.format("src/ensemble/session/b.py")
            + "@@ -0,0 +1,3 @@\n+x\n+y\n+z\n"
            + diff("tests/utils/unit/a_test.py", "-1 +1")
            + diff("src/ensemble/utils/config.toml", "-1 +1")
            + diff("outside/c.py", "-1 +1")
            + "diff --git a/src/ensemble/utils/gone.py b/src/ensemble/utils/gone.py\n"
            "deleted file mode 100644\n--- a/src/ensemble/utils/gone.py\n+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n-x\n-y\n"
        )
        self.assertEqual(
            gate.changed_lines(text, TREES),
            {
                "src/ensemble/utils/a.py": {4, 5, 12},
                "src/ensemble/session/b.py": {1, 2, 3},
            },
        )

    def test_hunk_bodies_never_start_a_file(self):
        text = diff("src/ensemble/utils/a.py", "-1 +1") + "+++ b/src/x.py\n"
        self.assertEqual(gate.changed_lines(text, TREES), {"src/ensemble/utils/a.py": {1}})


class Verdict(unittest.TestCase):
    def test_below_the_threshold_fails_listing_uncovered_lines_and_partial_branches(self):
        report = {
            "src/a.py": entry(
                executed=(1, 2, 3, 10), missing=(4, 5, 6, 8), branches=[(3, 10)], partial=[(3, -1)]
            ),
            "src/b.py": entry(executed=(1,)),
        }
        changed = {"src/a.py": {2, 3, 4, 5, 6, 8, 9}, "src/b.py": {1}}
        self.assertEqual(
            gate.verdict(report, changed),
            (
                1,
                [
                    "diff coverage: 44.4% (4/9) on 8 changed src lines; threshold 85%",
                    "src/a.py: lines 4-6, 8; branches 3->exit",
                ],
            ),
        )

    def test_at_or_above_the_threshold_passes(self):
        for covered, expected in ((17, "85.0% (17/20)"), (20, "100.0% (20/20)")):
            with self.subTest(covered=covered):
                report = {
                    "src/a.py": entry(
                        executed=range(1, covered + 1), missing=range(covered + 1, 21)
                    )
                }
                self.assertEqual(
                    gate.verdict(report, {"src/a.py": set(range(1, 21))}),
                    (0, [f"diff coverage: {expected} on 20 changed src lines; threshold 85%"]),
                )

    def test_the_percentage_rounds_down(self):
        report = {"src/a.py": entry(executed=range(1, 569), missing=range(569, 670))}
        code, lines = gate.verdict(report, {"src/a.py": set(range(1, 670))})
        self.assertEqual(code, 1)
        self.assertEqual(
            lines[0], "diff coverage: 84.9% (568/669) on 669 changed src lines; threshold 85%"
        )

    def test_no_changed_statements_pass(self):
        for changed in ({}, {"src/a.py": {7}}):
            with self.subTest(changed=changed):
                report = {"src/a.py": entry(executed=(1, 7), missing=(2,), excluded=(7,))}
                self.assertEqual(gate.verdict(report, changed), (0, ["no changed src lines"]))

    def test_unchanged_lines_and_their_branches_do_not_count(self):
        report = {
            "src/a.py": entry(executed=(1, 2), missing=(3,), branches=[(1, 2)], partial=[(1, 3)])
        }
        self.assertEqual(
            gate.verdict(report, {"src/a.py": {2}}),
            (0, ["diff coverage: 100.0% (1/1) on 1 changed src lines; threshold 85%"]),
        )

    def test_a_changed_continuation_line_counts_its_statement(self):
        report = {"src/a.py": {**entry(executed=(1,), missing=(2,)), "multiline": {3: 2}}}
        self.assertEqual(
            gate.verdict(report, {"src/a.py": {3}}),
            (
                1,
                [
                    "diff coverage: 0.0% (0/1) on 1 changed src lines; threshold 85%",
                    "src/a.py: lines 2",
                ],
            ),
        )


class Report(unittest.TestCase):
    """The report the gate reads: only registered `no cover` pragmas exclude code."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        source = self.root / "src/m.py"
        source.parent.mkdir(parents=True)
        source.write_text(
            "def f(x):\n    if x:  # pragma: no branch\n        return 1\n    return 2\n\n\n"
            "def g():  # pragma: " + "no cover\n    return 3\n"
        )
        (self.root / "src/never.py").write_text("A = 1\nB = 2\n")
        (self.root / "src/call.py").write_text("x = max(\n    1,\n    2,\n)\n")

    def report(self, arcs):
        try:
            import coverage
        except ImportError:
            self.skipTest("coverage.py is unavailable; the Coverage CI job runs this")
        data = coverage.CoverageData(basename=str(self.root / "data"))
        data.add_arcs({str(self.root / path): set(pairs) for path, pairs in arcs.items()})
        data.write()
        return gate.load_report(
            str(self.root / "data"),
            ["src/m.py", "src/never.py", "src/call.py"],
            self.root,
        )

    def test_no_branch_still_counts_the_missing_arc_and_no_cover_excludes(self):
        arcs = {"src/m.py": [(-1, 1), (1, 7), (7, -1), (-1, 2), (2, 3), (3, -1)]}
        report = self.report(arcs)
        self.assertEqual(
            {key: report["src/m.py"][key] for key in entry()},
            entry(
                executed=(1, 2, 3, 7),
                missing=(4,),
                branches=[(2, 3)],
                partial=[(2, 4)],
                excluded=(7, 8),
            ),
        )

    def test_a_file_no_test_imported_counts_every_statement_as_missing(self):
        report = self.report({"src/m.py": [(-1, 1)]})
        self.assertEqual(report["src/never.py"]["executed_lines"], [])
        self.assertEqual(report["src/never.py"]["missing_lines"], [1, 2])

    def test_continuation_lines_map_to_their_statements_first_line(self):
        report = self.report({"src/m.py": [(-1, 1)]})
        self.assertEqual(report["src/call.py"]["multiline"], {2: 1, 3: 1, 4: 1})
        self.assertEqual(report["src/m.py"]["multiline"], {})
