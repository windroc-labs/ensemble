import io
import os
import re
import runpy
import sys
from pathlib import Path
from unittest import mock

from ensemble.delegate.composer.base import TEAM
from tests.delegate.case import Case
from tests.delegate.team_tools import tool

SCRIPT = os.path.join(TEAM, "tools", "report_check.py")
SAMPLE = Path(__file__).with_name("fixtures") / "research-report.md"
report_check = tool("report_check")
LABEL = "Findings: 1 aspects, 1 findings"
ASPECT = "---\n\n### 1. Aspect\n\n"


class ReportCheck(Case):
    def test_template_limits_and_complete_example(self):
        template = Path(TEAM, "templates", "research-report.md").read_text(encoding="utf-8")
        self.assertIn(f"lists/paragraphs {report_check.LINE_LIMIT}", template)
        self.assertIn(f"other cells {report_check.CELL_LIMIT}", template)
        self.assertIn(f"this whole block at most {report_check.FINDINGS_LIMIT}", template)
        for row in (
            f"| Aspect answer | {report_check.ANSWER_LIMIT} |",
            f"| Orientation | {report_check.ORIENTATION_LIMIT} |",
            f"| Claim heading | {report_check.CLAIM_LIMIT} |",
            f"| Finding body | {report_check.BODY_SENTENCES} sentences, {report_check.BODY_LIMIT} |",
            f"| One aspect, everything in it | {report_check.ASPECT_LIMIT} |",
            f"| Code example | {report_check.CODE_LINES} lines (exempt from columns) |",
            f"| Table cells | {report_check.CELL_LIMIT}; last column {report_check.LINE_LIMIT} |",
        ):
            self.assertIn(row, template)
        self.assertNotIn("Filled example", template)
        sample = SAMPLE.read_text(encoding="utf-8").strip()
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
        self.assertEqual(sample.count('<details id="findings" open>'), 1)
        self.assertEqual(sample.count("\n\n---\n\n### "), 4)
        body, sources = sample.split("\n## Sources\n")
        self.assertNotRegex(body, r"(?<!\[)\[\d+\](?!\])")
        for number, target in re.findall(r"^(\d+)\. \[[^\]]+\]\(([^)]+)\)", sources, re.M):
            self.assertIn(f"[[{number}]]({target})", body)
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

    def test_table_cells_skip_link_targets(self):
        url = "https://github.com/example/" + "repo" * 20
        linked = "| Need | [" + "x" * 30 + "](" + url + ") | B |\n|---|---|---|"
        self.assertEqual(self.check(linked), (0, "ok\n"))
        code, out = self.check("| Need | [" + "x" * 31 + "](" + url + ") | B |\n|---|---|---|")
        self.assertEqual(code, 1)
        self.assertIn("line 1: cell 31>30:", out)

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

    def findings(self, body, tag='<details id="findings" open>'):
        return f"## Findings\n\n{tag}\n<summary>{LABEL}</summary>\n\n{body}\n\n</details>"

    def test_findings_block_has_one_total_budget(self):
        limit = report_check.FINDINGS_LIMIT
        findings = self.findings
        self.assertEqual(self.check(findings("**claim**\n\n" + "中" * 250)), (0, "ok\n"))
        rest = "x" * (limit - len(LABEL) - 10)
        self.assertEqual(self.check(findings("- " + "y" * 10 + "\n\n" + rest)), (0, "ok\n"))
        code, out = self.check(findings("- " + "y" * 11 + "\n\n" + rest))
        self.assertEqual(code, 1)
        first = f"{LABEL} {'y' * 11} {rest}"[:60]
        self.assertEqual(out, f"line 3: findings {limit + 1}>{limit}: {first}\n")
        table = "x" * (limit - len(LABEL)) + "\n\n| A | B |\n|---|---|\n| y | z |"
        self.assertIn(f"findings {limit + 4}>{limit}", self.check(findings(table))[1])
        claim = "x" * (limit - len(LABEL) - 80) + "{}\n\n#### " + "c" * 80
        self.assertEqual(self.check(findings(claim.format(""))), (0, "ok\n"))
        self.assertIn(f"findings {limit + 1}>{limit}", self.check(findings(claim.format("x")))[1])
        exempt = findings(f"```mermaid\n{rest}\n```\n\n<p>\n\n" + "x" * (limit - len(LABEL)))
        self.assertEqual(self.check(exempt), (0, "ok\n"))
        nested = "<details>\n<summary>More</summary>\n\nok\n\n</details>\n\n" + "x" * 100
        self.assertEqual(self.check(findings(nested)), (0, "ok\n"))
        for after, kind in (("x" * 81, "paragraph"), ("<details>\n\n- " + "x" * 81, "list")):
            with self.subTest(after=after):
                self.assertIn(f"{kind} 81>80:", self.check(findings("ok") + "\n\n" + after)[1])
        self.assertIn("paragraph 81>80:", self.check(findings("x" * 81, tag="<details>"))[1])
        unclosed = findings("x" * limit).removesuffix("\n\n</details>")
        self.assertIn(f"findings {limit + len(LABEL)}>{limit}", self.check(unclosed)[1])
        for end in ("x</details>", "x"):
            with self.subTest(end=end):
                text = findings(end).removesuffix("\n\n</details>") + "\n\n## Next\n\n- " + "x" * 81
                self.assertEqual(self.check(text), (1, "line 10: list 81>80: " + "x" * 60 + "\n"))

    def test_aspect_parts_have_their_own_limits(self):
        # "Answer: " is 8 columns; "1.1 " is 4
        cases = (
            (
                "> **Answer:** " + "a" * 152,
                "> **Answer:** " + "a" * 153,
                "line 10: answer 161>160:",
            ),
            (
                "> **Answer:** " + "a" * 152 + "\n> [[1]](https://example.com/" + "u" * 99 + ")",
                "",
                "",
            ),
            (
                "o" * 100 + "\n\n- " + "o" * 60,
                "o" * 100 + "\n\n- " + "o" * 61,
                "line 10: orientation 161>160:",
            ),
            (
                "> **Answer:** a\n\n" + "o" * 160,
                "> **Answer:** a\n\n" + "o" * 161,
                "line 12: orientation 161>160:",
            ),
            (
                "> **Answer:** a\n\n> " + "o" * 160,
                "> **Answer:** a\n\n> " + "o" * 161,
                "line 12: orientation 161>160:",
            ),
            ("#### 1.1 " + "c" * 76, "#### 1.1 " + "c" * 77, "line 10: claim 81>80:"),
            (
                "#### 1.1 c\n\n" + "b" * 400 + "\n\n- " + "b" * 74 + "\n\n(high)",
                "#### 1.1 c\n\n" + "b" * 400 + "\n\n- " + "b" * 75 + "\n\n(high)",
                "line 10: finding 481>480:",
            ),
            ("#### 1.1 c\n\n" + "b" * 480 + " [[1]](https://example.com/" + "u" * 99 + ")", "", ""),
            (
                "#### 1.1 c\n\n" + "Bb. " * 5 + "(high)",
                "#### 1.1 c\n\n" + "Bb. " * 6 + "(high)",
                "line 10: sentences 6>5:",
            ),
            (
                "| A | B |\n|---|---|\n| "
                + "x" * 30
                + " [[2]](https://example.com/"
                + "u" * 99
                + ") | "
                + "y" * 80
                + " |",
                "| A | B |\n|---|---|\n| " + "x" * 31 + " | y |",
                "line 12: cell 31>30:",
            ),
            (
                "```python\n" + "x = 1\n" * 15 + "```",
                "```python\n" + "x = 1\n" * 16 + "```",
                "line 10: code 16>15: x = 1",
            ),
            ("```mermaid\n" + "A --> B\n" * 40 + "```", "", ""),
        )
        for good, bad, failure in cases:
            with self.subTest(good=good[:40]):
                self.assertEqual(self.check(self.findings(ASPECT + good)), (0, "ok\n"))
                if bad:
                    code, out = self.check(self.findings(ASPECT + bad))
                    self.assertEqual(code, 1)
                    self.assertEqual(len(out.splitlines()), 1, out)
                    self.assertTrue(out.startswith(failure), out)
        self.assertEqual(self.check("```python\n" + "x = 1\n" * 40 + "```"), (0, "ok\n"))
        self.assertRegex(self.check(self.findings("```\n" + "x\n" * 16))[1], r"code \d+>15:")

    def test_sentences_count_terminators_not_abbreviations(self):
        body = (
            "Use e.g. a U.S. vendor, i.e. one vs. two, cf. Smith et al. in v0.4.0 and auth.json; "
            'see 2.1–2.3 [3]. 甲。乙！丙？ "Quoted." Wait?! (high)'
        )
        five = self.findings(ASPECT + "#### 1.1 c\n\n" + body.replace(" Wait?!", ""))
        self.assertEqual(self.check(five), (0, "ok\n"))
        self.assertIn(
            "sentences 6>5:", self.check(self.findings(ASPECT + "#### 1.1 c\n\n" + body))[1]
        )

    def test_each_aspect_has_one_budget_for_everything_in_it(self):
        limit = report_check.ASPECT_LIMIT

        def aspect(number, extra):
            parts = [f"---\n\n### {number}. Aspect\n\n> **Answer:** " + "a" * 92]
            parts += [f"#### {number}.{n} " + "c" * 46 + "\n\n" + "b" * 400 for n in range(1, 8)]
            parts.append(f"#### {number}.8 " + "c" * 46 + "\n\n" + "b" * (200 + extra))
            parts.append("| A | B |\n|---|---|\n| y | z |")
            return "\n\n".join(parts)

        # answer 100 + 8 claims of 50 + 7 bodies of 400 + last body 200 + 4 cells = 3504 + extra
        self.assertEqual(
            self.check(self.findings(aspect(1, -4) + "\n\n" + aspect(2, -4))), (0, "ok\n")
        )
        code, out = self.check(self.findings(aspect(1, -4) + "\n\n" + aspect(2, -3)))
        self.assertEqual(code, 1)
        self.assertRegex(out, rf"^line \d+: aspect {limit + 1}>{limit}: Answer: a+\n$")
        self.assertIn(
            f"line 8: aspect {limit + 1}>{limit}:", self.check(self.findings(aspect(1, -3)))[1]
        )

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
