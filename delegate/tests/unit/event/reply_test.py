"""delegate/event/reply.py: progress mark lines and the outcome block of a reply (FR-18)."""

import unittest

from ensemble.delegate.event.reply import marks, outcome_block, outcome_only

from delegate.tests.case import Case


class Marks(Case):
    def test_mark_lines_give_their_name_and_text_in_order(self):
        for text, want in (
            (
                "Reading the task.\n[ensemble-progress:start] first build of the PRD\nNow the spec.",
                [("start", "first build of the PRD")],
            ),
            (
                "- [ensemble-progress:a] one\n* [ensemble-progress:b] two\n   [ensemble-progress:c] three",
                [("a", "one"), ("b", "two"), ("c", "three")],
            ),
            (
                "[ensemble-progress:start]   first build \t of  the PRD  \r",
                [("start", "first build of the PRD")],
            ),
            ("[ensemble-progress:start] first build\n  of the PRD", [("start", "first build")]),
            (
                "[ensemble-progress:start] a\nx\n[ensemble-progress:step] b\n[ensemble-progress:start] c",
                [("start", "a"), ("step", "b"), ("start", "c")],
            ),
        ):
            with self.subTest(text=text):
                self.assertEqual(marks(text), want)

    def test_a_mark_and_an_outcome_block_are_one_mark_and_the_block(self):
        text = '[ensemble-progress:start] the build\nDone.\n```ensemble-outcome\n{"status": "done"}\n```\n'
        self.assertEqual(marks(text), [("start", "the build")])
        self.assertEqual(outcome_block(text), '{"status": "done"}')

    def test_a_mark_in_a_fence_or_not_at_the_line_start_or_no_text_is_none(self):
        for text in (
            "```\n[ensemble-progress:start] x\n```",
            "~~~md\n[ensemble-progress:start] x\n~~~",
            "```ensemble-outcome\n[ensemble-progress:start] x\n```",
            "````\n```\n[ensemble-progress:a] x",
            "see [ensemble-progress:start] x",
            "+ [ensemble-progress:start] x",
            "-[ensemble-progress:start] x",
            "`[ensemble-progress:start] x`",
            "",
        ):
            with self.subTest(text=text):
                self.assertEqual(marks(text), [])

    def test_a_bad_name_or_empty_text_is_skipped(self):
        for text in (
            "[ensemble-progress:a b] x",
            "[ensemble-progress:] x",
            "[ensemble-progress:a.b] x",
            "[ensemble-progress:start]",
            "[ensemble-progress:start]  \t ",
        ):
            with self.subTest(text=text):
                self.assertEqual(marks(text), [])
        self.assertEqual(marks("[ensemble-progress:plan-ready_2] ok"), [("plan-ready_2", "ok")])


class OutcomeOnly(Case):
    def test_a_reply_with_outcome_blocks_is_its_last_block_alone(self):
        body = '{"status": "done", "title": "T", "summary": "S"}'
        for text in (
            f"Built X.\n```ensemble-outcome\n{body}\n```\nDone.",
            f"```ensemble-outcome\n{{}}\n```\n```ensemble-outcome\n{body}\n```",
            f"```ensemble-outcome\n{body}\n```",
        ):
            with self.subTest(text=text):
                only = outcome_only(text)
                self.assertEqual(only, f"```ensemble-outcome\n{body}\n```")
                self.assertEqual(outcome_block(only), body)

    def test_a_reply_without_an_outcome_block_is_unchanged(self):
        for text in ("", "Working.", "```json\n{}\n```", "[ensemble-progress:start] go"):
            with self.subTest(text=text):
                self.assertEqual(outcome_only(text), text)

    def test_a_body_holding_backticks_gets_a_longer_fence(self):
        body = '{"deliverable": "a"}\n````\n'
        text = f"Prose.\n`````ensemble-outcome\n{body}`````"
        only = outcome_only(text)
        self.assertTrue(only.startswith("`````ensemble-outcome\n"), only)
        self.assertEqual(outcome_block(only), outcome_block(text))


class OutcomeBlock(Case):
    def test_the_last_blocks_text(self):
        for text, want in (
            (
                '```ensemble-outcome\n{"a": 1}\n```\nthen\n~~~ensemble-outcome\n{"b": 2}\n~~~\n',
                '{"b": 2}',
            ),
            ('x\n```ensemble-outcome\n{"a": 1}\n', '{"a": 1}\n'),
            ("```ensemble-outcome\n```", ""),
            ("```  ensemble-outcome \t\n{}\n```", "{}"),
        ):
            with self.subTest(text=text):
                self.assertEqual(outcome_block(text), want)

    def test_no_block_or_one_nested_in_a_longer_fence_is_none(self):
        for text in (
            "",
            "no block",
            "```\n{}\n```",
            "ensemble-outcome {}",
            "````md\n```ensemble-outcome\n{}\n```\n````\n",
        ):
            with self.subTest(text=text):
                self.assertIsNone(outcome_block(text))

    def test_a_block_with_another_info_string_is_ignored(self):
        text = (
            '```ensemble-outcome\n{"a": 1}\n```\n```json\n{}\n```\n```ensemble-outcome x\n{}\n```\n'
        )
        self.assertEqual(outcome_block(text), '{"a": 1}')
        self.assertIsNone(outcome_block("```ensemble-outcomes\n{}\n```"))


if __name__ == "__main__":
    unittest.main()
