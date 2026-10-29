import io
import os
import runpy
import sys
from pathlib import Path
from unittest import mock

from ensemble.delegate.composer.base import TEAM
from tests.delegate.case import Case
from tests.delegate.team_tools import tool

SCRIPT = os.path.join(TEAM, "tools", "report_check.py")
report_check = tool("report_check")


class ReportCheck(Case):
    def test_template_limits_and_complete_example(self):
        template = Path(TEAM, "templates", "research-report.md").read_text(encoding="utf-8")
        self.assertIn(f"lists/paragraphs {report_check.LINE_LIMIT}", template)
        self.assertIn(f"other cells {report_check.CELL_LIMIT}", template)
        self.assertIn(f"at most {report_check.FINDINGS_LIMIT} in total", template)
        sample = template.split("## Filled example\n", 1)[1].strip()
        self.assertEqual(self.check(sample), (0, "ok\n"))
        visible = []
        hidden = False
        for line in sample.splitlines():
            if line.startswith("<details"):
                hidden = True
            if not hidden or line.startswith("<summary>"):
                visible.append(line)
            if line.startswith("</details>"):
                hidden = False
        self.assertLessEqual(sum(not c.isspace() for c in "".join(visible)), 1500)
        self.assertEqual(sample.count("<details>"), 1)
        self.assertEqual(sample.count('<details id="findings">'), 1)
        self.assertIn("## What it means for us", sample)
        self.assertIn("```mermaid", sample)
        self.assertRegex(
            sample,
            r"^# .+\n\nLight Research: light-research · mixed · windroc-labs/ensemble@[a-f0-9]{12}\n",
        )

    def check(self, text):
        path = Path(self.home, "report.md")
        path.write_text(text, encoding="utf-8")
        out = io.StringIO()
        code = report_check.main([str(path)], out=out)
        self.assertEqual(path.read_text(encoding="utf-8"), text)
        return code, out.getvalue()

    def test_empty_and_exact_line_boundaries(self):
        for text in ("", "- " + "x" * 80, "中" * 40, "> - " + "😀" * 40):
            with self.subTest(text=text):
                self.assertEqual(self.check(text), (0, "ok\n"))
        for prefix, kind in (("- ", "list"), ("", "paragraph"), ("> - ", "list")):
            with self.subTest(prefix=prefix):
                code, out = self.check(prefix + "x" * 81)
                self.assertEqual(code, 1)
                self.assertTrue(out.startswith(f"line 1: {kind} 81>80: "), out)

    def test_display_width_and_visible_markup(self):
        cases = (
            ("中" * 40 + "a", 81),
            ("😀" * 40 + "a", 81),
            ("Ａ" * 40 + "a", 81),
            ("é" * 81, 81),
            ("`" + "a_b" * 27 + "`", 81),
            ("a_b" * 27, 81),
        )
        for text, width in cases:
            with self.subTest(text=text):
                self.assertIn(f"paragraph {width}>80:", self.check(text)[1])
        url = "https://example.com/" + "long" * 100
        for text in (
            "- **" + "中" * 40 + "** [1][20] " + url,
            "- [" + "中" * 40 + "](" + url + "(part))",
            "- [" + "中" * 40 + "][source]",
            "[source]: " + url,
            "> [!IMPORTANT]\n>\n> - **" + "中" * 40 + "** [1]",
            "- " + "a" * 79 + "&amp;",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.check(text), (0, "ok\n"))

    def test_tables_check_headers_and_every_cell(self):
        good = "| " + "中" * 15 + " | " + "x" * 80 + " |\n| :--- | ---: |"
        self.assertEqual(self.check(good), (0, "ok\n"))
        bad = "| " + "x" * 31 + " | " + "中" * 41 + " |\n| --- | --- |"
        code, out = self.check(bad)
        self.assertEqual(code, 1)
        self.assertEqual(len(out.splitlines()), 2)
        self.assertIn("line 1: cell 31>30:", out)
        self.assertIn("line 1: cell 82>80:", out)
        for row in ("| a\\|b | `x|y` |", "a | b", "| a | b | c |"):
            count = 3 if " c " in row else 2
            self.assertEqual(self.check(row + "\n" + "|---" * count + "|"), (0, "ok\n"))
        code, out = self.check("| a | b |\n|---|---|\n| " + "x" * 31 + " | short |")
        self.assertEqual(code, 1)
        self.assertIn("line 3: cell 31>30:", out)
        for row in (
            "| " + "x" * 31 + " |",
            "x" * 31,
            "| " + "x" * 15 + "\\|" + "y" * 15 + " | short |",
            "| `" + "x" * 15 + "|" + "y" * 15 + "` | short |",
        ):
            with self.subTest(row=row):
                self.assertIn("line 3: cell 31>30:", self.check("| a | b |\n|---|---|\n" + row)[1])
        self.assertIn("paragraph 83>80:", self.check("x|" + "a" * 81)[1])

    def test_table_boundaries_and_missing_or_extra_cells(self):
        header = "| A | B |\n|---|---|\n"
        for following in ("- " + "x" * 80, "> " + "x" * 80, "\n" + "x" * 80):
            with self.subTest(following=following):
                self.assertEqual(self.check(header + following), (0, "ok\n"))
        self.assertEqual(self.check(header + "| ok | ok | " + "x" * 100 + " |"), (0, "ok\n"))
        self.assertEqual(self.check("| " + "x" * 31 + " | B |\n|---|"), (0, "ok\n"))
        self.assertIn("cell 31>30:", self.check("| A | B |\n|-|-|\n" + "x" * 31)[1])
        self.assertIn("cell 31>30:", self.check("> | A | B |\n> |---|---|\n> " + "x" * 31)[1])
        self.assertEqual(
            self.check("| A | B\\\\|\n|---|---|\n| ok | " + "x" * 80 + " |"), (0, "ok\n")
        )

    def test_fences_headings_types_and_html(self):
        long = "x" * 200
        text = (
            f"# {long}\n\nResearch: research · mixed · {long}@123456789abc\n\n"
            f"````python\n{long}\n```\n{long}\n````\n"
            f"~~~mermaid\n{long}\n~~~\n"
            f'<details data-title="{long}">\n<summary>Findings (1)</summary>\n\n'
            "- short\n\n</details>"
        )
        self.assertEqual(self.check(text), (0, "ok\n"))
        for text in (
            "<details>\n\n- " + "x" * 81 + "\n\n</details>",
            "<p>" + "x" * 81 + "</p>",
            "# H\n\n- ok\n\nResearch: research · web · " + long,
            "- <https://example.com/" + long + "> " + "x" * 81,
        ):
            with self.subTest(text=text):
                self.assertEqual(self.check(text)[0], 1)

    def test_soft_wraps_do_not_reset_the_budget(self):
        for text, kind in (
            ("a" * 40 + "\n" + "b" * 40, "paragraph"),
            ("- " + "a" * 40 + "\n  " + "b" * 40, "list"),
            ("> - " + "a" * 40 + "\n>   " + "b" * 40, "list"),
        ):
            with self.subTest(text=text):
                self.assertIn(f"line 1: {kind} 81>80:", self.check(text)[1])
        self.assertEqual(self.check("- " + "a" * 80 + "\n- " + "b" * 80), (0, "ok\n"))

    def test_findings_block_has_one_budget_instead_of_line_caps(self):
        limit = report_check.FINDINGS_LIMIT
        label = "Findings (1)"

        def findings(body, tag='<details id="findings">'):
            return f"## Findings\n\n{tag}\n<summary>{label}</summary>\n\n{body}\n\n</details>"

        self.assertEqual(self.check(findings("**claim**\n\n" + "中" * 250)), (0, "ok\n"))
        rest = "x" * (limit - len(label) - 10)
        self.assertEqual(self.check(findings("- " + "y" * 10 + "\n\n" + rest)), (0, "ok\n"))
        code, out = self.check(findings("- " + "y" * 11 + "\n\n" + rest))
        self.assertEqual(code, 1)
        first = f"{label} {'y' * 11} {'x' * 35}"
        self.assertEqual(out, f"line 3: findings {limit + 1}>{limit}: {first}\n")
        half = "x" * (limit // 2)
        table = f"| A | B |\n|---|---|\n| {half} | {half} |"
        self.assertIn(f"findings {limit + len(label) + 2}>{limit}", self.check(findings(table))[1])
        exempt = f"### {rest}\n\n```mermaid\n{rest}\n```\n\n<p>\n\nok"
        self.assertEqual(self.check(findings(exempt)), (0, "ok\n"))
        nested = "<details>\n<summary>More</summary>\n\nok\n\n</details>\n\n" + "x" * 100
        self.assertEqual(self.check(findings(nested)), (0, "ok\n"))
        for after, kind in (("x" * 81, "paragraph"), ("<details>\n\n- " + "x" * 81, "list")):
            with self.subTest(after=after):
                self.assertIn(f"{kind} 81>80:", self.check(findings("ok") + "\n\n" + after)[1])
        self.assertIn("paragraph 81>80:", self.check(findings("x" * 81, tag="<details>"))[1])
        unclosed = findings("x" * limit).removesuffix("\n\n</details>")
        self.assertIn(f"findings {limit + len(label)}>{limit}", self.check(unclosed)[1])
        for end in ("x</details>", "x"):
            with self.subTest(end=end):
                text = findings(end).removesuffix("\n\n</details>") + "\n\n## Next\n\n- " + "x" * 81
                self.assertEqual(self.check(text), (1, "line 10: list 81>80: " + "x" * 60 + "\n"))

    def test_usage_read_errors_and_entrypoint(self):
        for args in ([], ["a", "b"], [os.path.join(self.home, "missing")], [self.home]):
            with self.subTest(args=args):
                out = io.StringIO()
                self.assertEqual(report_check.main(args, out=out), 2)
                self.assertTrue(out.getvalue().startswith("check failed: "), out.getvalue())
        bad = Path(self.home, "bad.md")
        bad.write_bytes(b"\xff")
        out = io.StringIO()
        self.assertEqual(report_check.main([str(bad)], out=out), 2)
        self.assertTrue(out.getvalue().startswith("check failed: "))
        with (
            mock.patch.object(sys, "argv", [SCRIPT]),
            mock.patch("sys.stdout", io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            runpy.run_path(SCRIPT, run_name="__main__")
        self.assertEqual(raised.exception.code, 2)
