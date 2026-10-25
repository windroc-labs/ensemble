"""utils/markdown.py: unfenced lines and fenced blocks."""

import unittest

from ensemble.utils.markdown import fenced, unfenced
from tests.utils.case import Case


class Unfenced(Case):
    def test_skips_fences_by_char_and_length_and_two_backticks_are_no_fence(self):
        for text, want in (
            (
                "a\n```\nb\n~~~\n```\n````\n```\nc\n````\n~~~ js\nd\n~~~  \ne\n  ```\nf\n~~~\ng",
                [(0, "a"), (12, "e")],
            ),
            ("``\nx", [(0, "``"), (1, "x")]),
        ):
            with self.subTest(text=text):
                self.assertEqual(unfenced(text), want)


class Fenced(Case):
    def test_blocks_in_order_with_their_info_strings_stripped(self):
        for text, want in (
            (
                "a\n```  ensemble-outcome  \n{}\n```\nb\n~~~ js\nx\ny\n~~~\n```\n```\n",
                [("ensemble-outcome", "{}"), ("js", "x\ny"), ("", "")],
            ),
            ("```x\nbody\n`````\nafter\n```y\nz\n  ```  \n", [("x", "body"), ("y", "z")]),
            ("a\n````md\nx\n```\ny", [("md", "x\n```\ny")]),
            ("```\n", [("", "")]),
            (
                "````outer\n```inner\nx\n```\n~~~\n````\n~~~~\n```\n~~~\n~~~~\n",
                [("outer", "```inner\nx\n```\n~~~"), ("", "```\n~~~")],
            ),
            ("```\nx\n``` y\n```~\n```\n", [("", "x\n``` y\n```~")]),
            ("", []),
            ("plain\ntext", []),
            ("``\nx\n``", []),
            ("a ```b```", []),
        ):
            with self.subTest(text=text):
                self.assertEqual(fenced(text), want)


if __name__ == "__main__":
    unittest.main()
