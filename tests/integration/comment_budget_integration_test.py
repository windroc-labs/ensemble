"""Comment budget on sandboxed Git repos: each file changed since the base's merge-base."""

import contextlib
import io
import os
import tempfile
from pathlib import Path
from unittest import mock

import comment_budget as budget
from budget_fixtures import (
    CODE,
    DOC,
    INIT,
    PURPOSE,
    SOURCE_RULE,
    TESTS_RULE,
    chars,
    over,
)

from tests.case import Case
from tests.utils.sandbox import clean_env, git


def write(root, files):
    for path, text in dict(files).items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)


def commit(root, files, message):
    write(root, files)
    git("add", "-A", cwd=root)
    git("commit", "-q", "--allow-empty", "-m", message, cwd=root)
    return git("rev-parse", "HEAD", cwd=root)


class Main(Case):
    def setUp(self):
        home = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.dict(os.environ, {**clean_env(), "HOME": home}, clear=True))

    def run_main(self, root, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = budget.main(list(argv), root)
        return code, out.getvalue().splitlines(), err.getvalue()

    def run_in(self, changed, base=(), *, removed=(), uncommitted=(), untracked=()):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            git("init", "-q", cwd=root)
            fork = commit(root, {"README": "", **dict(base)}, "base")
            for path in removed:
                (root / path).unlink()
            commit(root, changed, "change")
            write(root, uncommitted)
            write(root, untracked)
            return self.run_main(root, "--base", fork)

    def test_under_budget_passes_with_a_summary_per_rule(self):
        code, lines, err = self.run_in(
            {"x.py": INIT, "core/a.py": CODE, "tests/a_test.py": CODE, "notes.txt": DOC}
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(lines, [f"ok {SOURCE_RULE}: 2 checked", f"ok {TESTS_RULE}: 1 checked"])

    def test_each_limit_plus_the_allowance_is_inclusive(self):
        body = "a = 0\n" * 100 + "x\n"
        for path, limit in (("core/a.py", 10), ("tests/a.py", 5)):
            allowed = limit * chars(body) // 100 + 300
            for extra, expected in ((0, 0), (1, 1)):
                with self.subTest(path=path, extra=extra):
                    comment = "#" + "c" * (allowed - 1 + extra)
                    self.assertEqual(self.run_in({path: body + comment + "\n"})[0], expected)

    def test_a_docstring_only_init_passes_within_the_allowance(self):
        self.assertEqual(chars(INIT), 300)
        self.assertEqual(self.run_in({"core/__init__.py": INIT})[0], 0)
        self.assertEqual(self.run_in({"core/__init__.py": INIT.replace("d", "dd", 1)})[0], 1)

    def test_unchanged_files_are_not_judged(self):
        code, lines, _ = self.run_in({"core/a.py": CODE}, {"core/old.py": DOC, "tests/b.py": DOC})
        self.assertEqual(
            (code, lines), (0, [f"ok {SOURCE_RULE}: 1 checked", f"ok {TESTS_RULE}: 0 checked"])
        )

    def test_each_changed_file_is_judged_alone(self):
        code, lines, _ = self.run_in({"core/a.py": CODE * 20, "core/b.py": DOC + CODE})
        self.assertLess(chars(DOC) * 100, 10 * 21 * chars(CODE))
        self.assertEqual(
            (code, lines[:2]),
            (
                1,
                [
                    f"FAIL {SOURCE_RULE}: 1 of 2 over budget",
                    over("core/b.py", chars(DOC), chars(CODE), 10),
                ],
            ),
        )

    def test_source_and_tests_are_judged_by_their_own_rule(self):
        code, lines, _ = self.run_in({"core/a.py": CODE * 10, "core/a_test.py": DOC})
        self.assertEqual(
            (code, lines),
            (
                1,
                [
                    f"ok {SOURCE_RULE}: 1 checked",
                    f"FAIL {TESTS_RULE}: 1 of 1 over budget",
                    over("core/a_test.py", chars(DOC), 0, 5),
                    PURPOSE,
                ],
            ),
        )

    def test_failure_lists_each_failing_file_with_its_numbers_then_the_purpose_once(self):
        n, code = chars(DOC), chars(CODE)
        status, lines, err = self.run_in(
            {
                "setup.py": DOC,
                "core/a.py": CODE,
                "core/x/b.py": DOC,
                "lib/c.py": DOC + CODE,
                "tests/a_test.py": CODE,
                "tests/unit/b_test.py": DOC,
            }
        )
        self.assertEqual((status, err), (1, ""))
        self.assertEqual(
            lines,
            [
                f"FAIL {SOURCE_RULE}: 3 of 4 over budget",
                over("core/x/b.py", n, 0, 10),
                over("lib/c.py", n, code, 10),
                over("setup.py", n, 0, 10),
                f"FAIL {TESTS_RULE}: 1 of 2 over budget",
                over("tests/unit/b_test.py", n, 0, 5),
                PURPOSE,
            ],
        )

    def test_changes_on_the_base_after_the_fork_are_not_judged(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            git("init", "-q", "-b", "main", cwd=root)
            commit(root, {"core/shared.py": DOC}, "fork")
            git("checkout", "-q", "-b", "topic", cwd=root)
            commit(root, {"core/a.py": CODE}, "topic")
            git("checkout", "-q", "main", cwd=root)
            commit(root, {"core/shared.py": CODE}, "base moves on")
            git("checkout", "-q", "topic", cwd=root)
            self.assertEqual(
                self.run_main(root, "--base", "main")[:2],
                (
                    0,
                    [
                        f"ok {SOURCE_RULE}: 1 checked",
                        f"ok {TESTS_RULE}: 0 checked",
                    ],
                ),
            )

    def test_uncommitted_edits_count_and_untracked_files_do_not(self):
        self.assertEqual(self.run_in({"core/a.py": CODE}, uncommitted={"core/a.py": DOC})[0], 1)
        self.assertEqual(self.run_in({"core/a.py": CODE}, untracked={"core/new.py": DOC})[0], 0)

    def test_deleted_files_are_skipped(self):
        code, lines, _ = self.run_in({}, {"core/old.py": DOC}, removed=["core/old.py"])
        self.assertEqual(
            (code, lines), (0, [f"ok {SOURCE_RULE}: 0 checked", f"ok {TESTS_RULE}: 0 checked"])
        )

    def test_a_syntax_error_exits_2_naming_the_file(self):
        code, lines, err = self.run_in({"core/a.py": CODE, "core/bad.py": "def f(:\n"})
        self.assertEqual((code, lines), (2, []))
        self.assertTrue(err.startswith("comment_budget: core/bad.py: "), err)

    def test_an_unknown_base_exits_2(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            git("init", "-q", cwd=root)
            commit(root, {"core/a.py": CODE}, "only")
            code, lines, err = self.run_main(root, "--base", "no-such-branch")
        self.assertEqual((code, lines), (2, []))
        self.assertTrue(err.startswith("comment_budget: git merge-base failed: "), err)
