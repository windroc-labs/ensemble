""".github/comment_budget.py: measure, rule, and main over temp git repos."""

import importlib.util
import os
import unittest

from budget_fixtures import chars

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "comment_budget.py")
spec = importlib.util.spec_from_file_location("comment_budget", PATH)
budget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget)


class Measure(unittest.TestCase):
    def test_comments_count_with_their_hash_but_a_shebang_and_ordinary_strings_do_not(self):
        strings = (
            'x = "an error message"\nprint("help text", x)\ny = f"{x} more"\n'
            'DOC = """assigned, not bare"""\ndef f():\n    return "a value"\n'
        )
        for text, expected in (
            ("x = 1  # a note\n", (chars("# a note"), 0, chars("x = 1"))),
            ("#!/usr/bin/env python3\nx = 1\n", (0, 0, chars("#!/usr/bin/env python3 x = 1"))),
            ("x = 1\n#! not a shebang\n", (chars("#! not a shebang"), 0, chars("x = 1"))),
            (strings, (0, 0, chars(strings))),
        ):
            with self.subTest(text=text):
                self.assertEqual(budget.measure(text), expected)

    def test_docstrings_and_other_bare_strings_count(self):
        strings = [
            '"""Module."""',
            "'''Class\n    over two lines.'''",
            '"Function."',
            '"inside an if"',
        ]
        text = (
            f"{strings[0]}\nclass A:\n    {strings[1]}\n    def f(self):\n        {strings[2]}\n"
            f"        if self:\n            {strings[3]}\n        return 1\n"
        )
        comment, docstring, code = budget.measure(text)
        self.assertEqual((comment, docstring), (0, sum(map(chars, strings))))
        self.assertEqual(code, chars(text) - docstring)

    def test_code_is_every_other_character(self):
        text = '"""Doc."""\nx = "s"  # note\n'
        comment, docstring, code = budget.measure(text)
        self.assertEqual(
            (comment, docstring, comment + docstring + code),
            (chars("# note"), chars('"""Doc."""'), chars(text)),
        )


class Rules(unittest.TestCase):
    def test_paths_pick_their_rule(self):
        for path in ("tests/a.py", "x/tests/b.py", "tests/unit/c.py", "delegate/d_test.py"):
            with self.subTest(path=path):
                self.assertIs(budget.rule(path), budget.TESTS)
        for path in ("src/e.py", ".github/comment_budget.py", "contests/f.py", "tests.py"):
            with self.subTest(path=path):
                self.assertIs(budget.rule(path), budget.SOURCE)


class CutOnly(unittest.TestCase):
    BEFORE = '"""Doc, long."""\ndef f(x):  # a note\n    return x * 2\n'

    def test_code_is_what_measure_counts_as_code(self):
        for text in (
            self.BEFORE,
            '#!/usr/bin/env python3\n# top\nx = ("é")\n"é doc"\nif x:\n    """inner"""\n',
            "x = 1\n#! not a shebang\n",
        ):
            with self.subTest(text=text):
                self.assertEqual(len(budget.code(text)), budget.measure(text)[2])
        self.assertEqual(budget.code(self.BEFORE), "deff(x):returnx*2")

    def test_same_code_and_no_more_comment_is_a_cut(self):
        for after in (
            '"""Doc."""\ndef f(x):  # a note\n    return x * 2\n',
            "def f(x):\n    return x * 2\n",
            self.BEFORE,
        ):
            with self.subTest(after=after):
                self.assertTrue(budget.cut_only(self.BEFORE, after))

    def test_new_files_more_comment_or_any_code_change_are_judged(self):
        for before, after in (
            (None, self.BEFORE),
            (self.BEFORE, self.BEFORE.replace("note", "longer note")),
            (self.BEFORE, self.BEFORE.replace("x * 2", "x - 3")),
            (self.BEFORE, self.BEFORE.replace("a note", "note").replace("x * 2", "x * 22")),
            (self.BEFORE, self.BEFORE.replace('"""Doc, long."""', 'D = "Doc, long."')),
        ):
            with self.subTest(after=after):
                self.assertFalse(budget.cut_only(before, after))
