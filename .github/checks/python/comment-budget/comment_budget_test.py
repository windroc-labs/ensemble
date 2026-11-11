import io
import subprocess
import tempfile
import unittest
from pathlib import Path

import comment_budget as budget
from comment_budget import Config, Counts, Error, Options, chars, glob, measure, parse

DEFAULT = """\
source_percent = 10
test_percent = 5
allowance_chars = 300
test_paths = ["**/tests/**", "**/test_*.py", "**/*_test.py"]
"""


def counts(comment, code):
    return Counts(chars(comment), chars(code))


def source(text):
    m = measure(text, whole_file_is_test=False)
    assert m.tests == Counts(), text
    return m.source


class Measuring(unittest.TestCase):
    def test_comments_docstrings_and_bare_strings_count(self):
        for text, comment, code in (
            ("x = 1  # note\n", "# note", "x = 1"),
            ('"""Module."""\nx = 1\n', '"""Module."""', "x = 1"),
            (
                "class A:\n    '''Class\n    doc.'''\n    def f(self):\n        \"Doc.\"\n        return 1\n",
                "'''Class doc.''' \"Doc.\"",
                "class A: def f(self): return 1",
            ),
            ("if x:\n    'bare'\n", "'bare'", "if x:"),
            ("x = 1\n#! not a shebang\n", "#! not a shebang", "x = 1"),
        ):
            with self.subTest(text=text):
                self.assertEqual(source(text), counts(comment, code))

    def test_ordinary_strings_are_never_comments(self):
        for text in (
            'x = "# not a comment"\n',
            'DOC = """assigned, not bare"""\n',
            'print("help # text")\ny = f"{x} # more"\n',
            'def f():\n    return "a value"\n',
            "b'bytes'\n",
        ):
            with self.subTest(text=text):
                self.assertEqual(source(text), counts("", text))

    def test_a_first_line_shebang_is_code(self):
        text = "#!/usr/bin/env python3\nx = 1  # why\n"
        self.assertEqual(source(text), counts("# why", "#!/usr/bin/env python3 x = 1"))

    def test_unparsable_files_are_errors(self):
        for text in ('"""open', "x = (", "def f(:\n"):
            with self.subTest(text=text):
                self.assertRaises(Error, measure, text, whole_file_is_test=False)

    def test_a_test_path_counts_the_whole_file_as_tests(self):
        m = measure("x = 1  # a\n", whole_file_is_test=True)
        self.assertEqual((m.source, m.tests), (Counts(), counts("# a", "x = 1")))


class Configuring(unittest.TestCase):
    def test_a_config_with_the_default_values_parses(self):
        self.assertEqual(
            budget.parse_config(DEFAULT),
            Config(10, 5, 300, ["**/tests/**", "**/test_*.py", "**/*_test.py"]),
        )

    def test_config_errors_name_the_problem(self):
        def with_(old, new):
            return DEFAULT.replace(old, new, 1)

        for text, error in (
            (with_("test_percent = 5\n", ""), "missing key `test_percent`"),
            (with_("test_paths", "test_globs"), "unknown key `test_globs`"),
            (f"{DEFAULT}test_percent = 1\n", "line 5"),
            (with_("= 10", '= "10"'), "`source_percent` must be an integer"),
            (with_("= 10", "= -1"), "`source_percent` must be an integer"),
            (with_("= 10", "= true"), "`source_percent` must be an integer"),
            (with_("= 300", "= 1.5"), "`allowance_chars` must be an integer"),
            (with_("= 5", '= ["5"]'), "`test_percent` must be an integer"),
            (with_('["**/tests/**", ', "[1, "), "`test_paths` must be a list of strings"),
            (with_("= [", '= "tests/**" #['), "`test_paths` must be a list of strings"),
            ("[section]\n", "unknown key `section`"),
            ("x = \n", "line 1"),
        ):
            with self.subTest(text=text):
                with self.assertRaises(Error) as caught:
                    budget.parse_config(text)
                self.assertIn(error, str(caught.exception))

    def test_test_path_globs_cross_directories_only_with_double_star(self):
        config = budget.parse_config(DEFAULT)
        for path in (
            "tests/a.py",
            "tests/unit/c.py",
            "pkg/tests/b.py",
            "test_d.py",
            "src/test_d.py",
            "src/d_test.py",
            "e_test.py",
        ):
            with self.subTest(path=path):
                self.assertTrue(config.is_test_path(path))
        for path in ("src/e.py", "contests/f.py", "tests.py", "src/test.py", "src/attest_x.py"):
            with self.subTest(path=path):
                self.assertFalse(config.is_test_path(path))
        self.assertTrue(glob("src/*.py", "src/a.py"))
        self.assertFalse(glob("src/*.py", "src/a/b.py"))
        self.assertTrue(glob("a/**/b.py", "a/b.py"))
        self.assertTrue(glob("*é*", "xéy"))

    def test_arguments_have_defaults_and_overrides(self):
        self.assertEqual(parse([]), Options("origin/main", None, all=False))
        self.assertEqual(
            parse(["--base", "HEAD", "--config", "c.toml", "--all"]),
            Options("HEAD", "c.toml", all=True),
        )
        for bad in ("--base", "--config", "--source-limit 1", "--nope"):
            with self.subTest(bad=bad):
                self.assertRaises(Error, parse, bad.split())


class Repo:
    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.git("init", "-q", "-b", "main")
        self.write(budget.CONFIG, DEFAULT)

    def git(self, *args):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
            + ["-c", "commit.gpgsign=false", "-C", str(self.root), *args],
            check=True,
        )

    def write(self, path, text):
        full = self.root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text)

    def run(self, args):
        out = io.StringIO()
        result = budget.run(args.split(), self.root, out)
        return result, out.getvalue()


class Running(unittest.TestCase):
    def repo(self):
        repo = Repo()
        self.addCleanup(repo.dir.cleanup)
        return repo

    def test_only_files_changed_since_the_merge_base_are_checked(self):
        repo = self.repo()
        repo.write("src/old.py", f"# {'x' * 400}\nx = 1\n")
        repo.git("add", ".")
        repo.git("commit", "-q", "-m", "base")
        repo.write("src/new.py", f"# {'y' * 298}\ny = 12\n")
        repo.write("tests/new.py", "t = 1\n")
        repo.git("add", ".")
        result, out = repo.run("--base main")
        self.assertTrue(result, out)
        self.assertTrue(
            out.startswith(
                "ok comment budget (source 10%, tests 5% of code + 300 characters "
                "per changed file): 2 checked\n"
            ),
            out,
        )
        repo.write("src/new.py", f"# {'y' * 301}\ny = 12\n")
        result, out = repo.run("--base main")
        self.assertFalse(result, out)
        self.assertIn("FAIL comment budget", out)
        self.assertIn(
            "src/new.py: 7550.00% (302 comment / 4 code characters, 0 in tests; "
            "300 allowed, 2 over)",
            out,
        )
        self.assertTrue(out.rstrip().endswith(budget.PURPOSE), out)
        self.assertNotIn("src/old.py:", out)

    def test_all_checks_every_tracked_file_and_reports_totals(self):
        repo = self.repo()
        repo.write("src/old.py", f"# {'x' * 400}\nx = 1\n")
        repo.write("tests/t.py", '"""ab"""\nt = 1\n')
        repo.git("add", ".")
        repo.git("commit", "-q", "-m", "base")
        result, out = repo.run("--all")
        self.assertFalse(result, out)
        self.assertIn("1 of 2 over budget", out)
        self.assertIn("total source: 401 comment / 3 code characters (13366.67%)", out)
        self.assertIn("total tests: 8 comment / 3 code characters (266.67%)", out)
        result, out = repo.run("--base main")
        self.assertTrue(result, out)
        self.assertIn(": 0 checked", out)

    def test_the_config_path_can_be_overridden_and_must_exist(self):
        repo = self.repo()
        repo.write("a.py", "x = 1\n")
        repo.write("strict.toml", DEFAULT.replace("= 300", "= 0"))
        repo.git("add", "a.py")
        repo.git("commit", "-q", "-m", "base")
        result, out = repo.run("--all --config strict.toml")
        self.assertTrue(result, out)
        self.assertIn("+ 0 characters per file", out)
        with self.assertRaises(Error) as caught:
            repo.run("--all --config missing.toml")
        self.assertTrue(str(caught.exception).startswith("config "))
        self.assertIn("missing.toml", str(caught.exception))
        (repo.root / budget.CONFIG).unlink()
        with self.assertRaises(Error) as caught:
            repo.run("--all")
        self.assertIn(budget.CONFIG, str(caught.exception))

    def test_a_bad_base_or_an_unparsable_file_is_an_error(self):
        repo = self.repo()
        repo.write("a.py", "x = 1\n")
        repo.git("add", ".")
        repo.git("commit", "-q", "-m", "base")
        self.assertRaises(Error, repo.run, "--base nope")
        repo.write("a.py", "x = (\n")
        with self.assertRaises(Error) as caught:
            repo.run("--all")
        self.assertTrue(str(caught.exception).startswith("a.py: "))


if __name__ == "__main__":
    unittest.main()
