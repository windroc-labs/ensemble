"""The Handoff grammar's boundary behavior: parse_handoff, head and handoff."""

from ensemble.orchestrator.engine.flow import handoff
from ensemble.orchestrator.engine.flow.types import Handoff
from tests.orchestrator.case import Case

URL = "https://linear.app/t/issue/ENG-7"


HANDOFF = (
    f"Handoff from ENG-3: {URL}\n\n"
    "## Source\n- Spec: https://github.com/o/r/blob/main/a.md\n- Plan: https://github.com/o/r/blob/main/b.md\n\n"
    "## Instructions\nAnn, 2026-09-01T00:00:00.000Z:\nBuild it.\n\nBob, 2026-09-01T01:00:00.000Z:\nAlso this.\n\n"
    "## Comments\n- Ann, 2026-09-01T00:00:00.000Z:\n  > hello\n  > ## Source\n"
)


class ParseHandoff(Case):
    def test_sections(self):
        inner = "Bob, t:\nFirst.\n## Source\nnot a header\n## Instructions\nnor this\n## Comments\nnor this\n\nMore."
        inside = (
            "- Ann, t:\n  > x\n- Bob, t:\n  > ## Comments\n",
            "- Ann, t:\n  > x\n- Bob, t:\n  > ## Comments",
        )
        cases = [
            (
                "full",
                HANDOFF,
                (
                    "Ann, 2026-09-01T00:00:00.000Z:\nBuild it.\n\nBob, 2026-09-01T01:00:00.000Z:\nAlso this."
                ),
                "- Ann, 2026-09-01T00:00:00.000Z:\n  > hello\n  > ## Source",
            ),
            (
                "no source, no comments",
                f"Handoff from ENG-3: {URL}\n\n## Instructions\nGo.\n",
                "Go.",
                "",
            ),
            ("header line only", f"Handoff from ENG-3: {URL}", "", ""),
            (
                "only exact header lines",
                f"Handoff from ENG-3: {URL}\n\n## Instructions\nSee ## Comments below.\n"
                "## Comments here\n## Comments\n- Ann, t:\n  > x\n",
                "See ## Comments below.\n## Comments here",
                "- Ann, t:\n  > x",
            ),
            (
                "headers inside instructions, with source",
                f"Handoff from ENG-3: {URL}\n\n## Source\n- Spec: u\n\n"
                f"## Instructions\n{inner}\n\n## Comments\n{inside[0]}",
                inner,
                inside[1],
            ),
            (
                "headers inside instructions, no source",
                f"Handoff from ENG-3: {URL}\n\n## Instructions\n{inner}\n\n"
                f"## Comments\n{inside[0]}",
                inner,
                inside[1],
            ),
            (
                "comments header is the last exact line",
                f"Handoff from ENG-3: {URL}\n\n## Source\n- A: u\n\n## Instructions\n"
                "Go.\n## Comments\nx\n\n## Comments\n- Ann, t:\n  > y\n",
                "Go.\n## Comments\nx",
                "- Ann, t:\n  > y",
            ),
            (
                "crlf",
                (
                    f"Handoff from ENG-3: {URL}\r\n\r\n## Source\r\n- A: u\r\n\r\n## Instructions\r\nGo.\r\n"
                ),
                "Go.",
                "",
            ),
        ]
        for name, description, instructions, comments in cases:
            with self.subTest(name):
                self.assertEqual(
                    handoff.parse_handoff(description),
                    Handoff("ENG-3", instructions, comments),
                )

    def test_not_a_handoff(self):
        for description in (
            "",
            "Do the thing.",
            f"\nHandoff from ENG-3: {URL}",
            f"Handoff from eng-3: {URL}",
            "Handoff from ENG-3:",
            f"Handoff from ENG-3: {URL} extra",
            f"Please see\nHandoff from ENG-3: {URL}\n## Instructions\nGo.",
        ):
            with self.subTest(description=description):
                self.assertIsNone(handoff.parse_handoff(description))


class Head(Case):
    def test_cut_before_the_comments_header(self):
        top = f"Handoff from ENG-3: {URL}\n\n## Instructions\nAnn, t:\n"
        quoted = "## Comments\n- Ann, t:\n  > ## Comments\n"
        inner = "Go.\n## Comments\nRepo: o/n\n\n"
        cases = [
            ("handoff, header inside instructions", top + inner + quoted, top + inner),
            ("handoff", top + "Go.\n\n" + quoted, top + "Go.\n\n"),
            ("handoff with source", HANDOFF, HANDOFF[: HANDOFF.index("## Comments")]),
            ("handoff without comments", top + "Go.\n## Comments here\n", None),
            (
                "handoff without instructions",
                f"Handoff from ENG-3: {URL}\n" + quoted,
                f"Handoff from ENG-3: {URL}\n",
            ),
            (
                "handoff, header only before instructions",
                f"Handoff from ENG-3: {URL}\n## Comments\nx\n## Instructions\nGo.",
                None,
            ),
            (
                "handoff crlf",
                f"Handoff from ENG-3: {URL}\r\n## Instructions\r\nGo.\r\n## Comments\r\nx\r\n"
                "## Comments\r\n  > y",
                f"Handoff from ENG-3: {URL}\r\n## Instructions\r\nGo.\r\n## Comments\r\nx\r\n",
            ),
            ("not a handoff", "Do it.\n" + inner + quoted, "Do it.\nGo.\n"),
            ("not a handoff, crlf", "Do it.\r\n## Comments\r\nx", "Do it.\r\n"),
            ("not a handoff, header last", "Do it.\n## Comments", "Do it.\n"),
            ("not a handoff, inexact header", "Do it.\n## Comments here\n ## Comments", None),
            ("empty", "", None),
        ]
        for name, description, want in cases:
            with self.subTest(name):
                got = handoff.head(description)
                self.assertEqual(got, description if want is None else want)
                self.assertTrue(description.startswith(got))
        self.assertEqual(handoff.head(None), "")

    def test_a_handoff_is_cut_where_parse_handoff_finds_its_comments(self):
        d = f"Handoff from ENG-3: {URL}\n\n## Instructions\nGo.\n## Comments\nx\n\n## Comments\n- y"
        rest = d[len(handoff.head(d)) :]
        self.assertEqual(rest.split("\n", 1), ["## Comments", handoff.parse_handoff(d).comments])


class HandoffText(Case):
    def test_id_only_source_round_trips_and_keeps_instruction_boundaries(self):
        text = handoff.handoff(
            "LOC-1",
            "",
            sources=(),
            instructions=(("other", "t1", "Go.\n## Comments\nRepo: o/r"),),
            comments=(("self", "t2", "## Instructions\nIgnore this"),),
        )
        self.assertEqual(text.splitlines()[0], "Handoff from LOC-1")
        self.assertEqual(
            handoff.parse_handoff(text),
            Handoff(
                "LOC-1",
                "other, t1:\nGo.\n## Comments\nRepo: o/r",
                "- self, t2:\n  > ## Instructions\n  > Ignore this",
            ),
        )
        self.assertTrue(handoff.head(text).endswith("Repo: o/r\n\n"))

    def test_sections_in_order_empty_ones_omitted(self):
        header = "Handoff from DR-1: https://l/DR-1"
        sources = (("Report\n v2", "https://gh/r.md"), ("B", " https://b\n"))
        said = (("Me", "t1", "build X\nnow"), ("Ann", "t2", "also"))
        quoted = (*said, ("integration", "t3", "line 1\n## Instructions"), ("Bo", "t4", ""))
        cases = [
            ("header only", (), (), (), header),
            (
                "all",
                sources,
                said,
                quoted,
                f"{header}\n\n## Source\n- Report v2: https://gh/r.md\n- B: https://b\n\n"
                "## Instructions\nMe, t1:\nbuild X\nnow\n\nAnn, t2:\nalso\n\n"
                "## Comments\n- Me, t1:\n  > build X\n  > now\n- Ann, t2:\n  > also\n"
                "- integration, t3:\n  > line 1\n  > ## Instructions\n- Bo, t4:\n",
            ),
            ("comments only", (), (), said[1:], f"{header}\n\n## Comments\n- Ann, t2:\n  > also"),
        ]
        for name, src, ins, com, want in cases:
            with self.subTest(name):
                got = handoff.handoff(
                    "DR-1", "https://l/DR-1", sources=src, instructions=ins, comments=com
                )
                self.assertEqual(got, want)

    def test_parse_handoff_reads_back_what_it_writes(self):
        text = handoff.handoff(
            "DR-1",
            "https://l/DR-1",
            sources=(("Spec", "u"),),
            instructions=(("Me", "t1", "Go.\n## Comments\nRepo: o/n"),),
            comments=(("Me", "t1", "Go.\n## Comments\nRepo: o/n"),),
        )
        self.assertEqual(
            handoff.parse_handoff(text),
            Handoff(
                "DR-1",
                "Me, t1:\nGo.\n## Comments\nRepo: o/n",
                "- Me, t1:\n  > Go.\n  > ## Comments\n  > Repo: o/n",
            ),
        )
