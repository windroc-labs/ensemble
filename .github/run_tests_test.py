""".github/run_tests.py: annotations, the step summary and crash detection, in-process and as CI
runs it.
"""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("run_tests", HERE / "run_tests.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class Cases:
    def test_pass(self):
        pass

    def test_fail(self):
        self.assertEqual(1, 2, "one\ntwo")

    def test_error(self):
        raise KeyError("k")

    def test_sub(self):
        with self.subTest(n=1):
            self.fail("a:b, c% |")


class Run(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.summary, self.state = self.tmp / "summary.md", self.tmp / "state"
        env = {
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "RUN_TESTS_STATE": str(self.state),
            "RUN_TESTS_DURATIONS": str(self.tmp / "durations.jsonl"),
        }
        self.enterContext(mock.patch.dict(os.environ, env))

    def run_tests(self, *names):
        out = io.StringIO()
        case = type("T", (Cases, unittest.TestCase), {})
        suite = unittest.TestSuite(case(name) for name in names)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = runner.run(suite, "Unit x")
        return code, out.getvalue()

    def test_failures_and_errors_are_annotated_by_test_id_and_tabled_in_the_summary(self):
        code, out = self.run_tests("test_pass", "test_fail", "test_error", "test_sub")
        self.assertEqual(code, 1)
        self.assertEqual(
            out.splitlines(),
            [
                f"::error title={__name__}.T.test_fail::AssertionError: 1 != 2 : one",
                f"::error title={__name__}.T.test_error::KeyError: 'k'",
                f"::error title={__name__}.T.test_sub::(n=1): AssertionError: a:b, c%25 |",
            ],
        )
        summary = self.summary.read_text()
        self.assertTrue(summary.startswith("### Unit x: 3 failed\n\n| Test | Error |\n"))
        self.assertIn(f"| `{__name__}.T.test_error` | KeyError: 'k' |", summary)
        self.assertIn(f"| `{__name__}.T.test_sub` | (n=1): AssertionError: a:b, c% \\| |", summary)
        self.assertEqual(self.state.read_text().splitlines()[-1], "failed")

    def test_success_prints_and_summarizes_nothing(self):
        self.assertEqual(self.run_tests("test_pass"), (0, ""))
        self.assertFalse(self.summary.exists())
        self.assertEqual(self.state.read_text(), f"start {__name__}.T.test_pass\npassed\n")

    def test_durations_are_recorded_outside_the_checkout(self):
        self.run_tests("test_pass")
        row = json.loads((self.tmp / "durations.jsonl").read_text())
        self.assertEqual(row["test"], f"{__name__}.T.test_pass")
        self.assertGreaterEqual(row["seconds"], 0)

    def test_annotation_text_is_escaped(self):
        self.assertEqual(runner.escape("a%\r\n:,"), "a%25%0D%0A:,")
        self.assertEqual(runner.escape("a%\r\n:,", prop=True), "a%25%0D%0A%3A%2C")


class Discovery(unittest.TestCase):
    def test_duration_shards_are_balanced_deterministic_complete_and_disjoint(self):
        cases = [unittest.FunctionTestCase(lambda: None) for _ in range(5)]
        for number, case in enumerate(cases):
            case.id = lambda number=number: f"sample.Case.test_{number}"
        durations = {cases[0].id(): 20, cases[1].id(): 10, "removed.test": 999}
        groups = [
            list(
                runner.tests(runner.shard(unittest.TestSuite(reversed(cases)), f"{i}/2", durations))
            )
            for i in (1, 2)
        ]
        self.assertCountEqual(
            [test.id() for group in groups for test in group], [test.id() for test in cases]
        )
        self.assertEqual(groups[0], [cases[0]])
        self.assertEqual(groups[1], cases[1:])
        self.assertEqual(
            [
                test.id()
                for test in runner.tests(runner.shard(unittest.TestSuite(cases), "1/2", durations))
            ],
            [test.id() for test in groups[0]],
        )
        for durations in (
            {"a": -1},
            {"a": 0},
            {"a": float("nan")},
            {"a": float("inf")},
            {"a": True},
            {"a": "1"},
            [],
        ):
            with self.subTest(durations=durations), self.assertRaises(ValueError):
                runner.shard(unittest.TestSuite(cases), "1/2", durations)

    def test_sharding_preserves_the_selected_suite_isolation_mode(self):
        for normalized in (False, True):
            root, path, _module = self.selection_fixture(normalized=normalized)
            gate = mock.Mock()
            with (
                mock.patch.object(runner, "supervise", return_value=None),
                mock.patch.object(runner.isolation, "install", return_value=gate),
                mock.patch.object(runner, "GATE"),
                mock.patch.object(runner.Path, "cwd", return_value=root),
                mock.patch.object(runner, "run", return_value=0) as run,
            ):
                self.assertEqual(runner.main(["Selected", str(path.parent), "--shard", "1/2"]), 0)
                self.assertEqual(run.call_args.args[0].countTestCases(), 1)
                self.assertEqual((gate.unit_only, gate.unit), (not normalized, not normalized))

    def test_parallel_jobs_cover_the_same_shards_without_supervising_the_coordinator(self):
        for title, selectors in (
            ("Repository", ["tests/integration", "--durations", "timings.json"]),
            ("Unit", ["--unit"]),
        ):
            with (
                self.subTest(title=title),
                mock.patch.object(runner, "parallel", return_value=0) as parallel,
            ):
                for number in (1, 2):
                    argv = [title, *selectors, "--parallel", "4", "--shard", f"{number}/2"]
                    self.assertEqual(runner.main(argv), 0)
                commands = [c for call in parallel.call_args_list for c in call.args[1].values()]
                self.assertEqual([argv[-1] for argv in commands], [f"{i}/8" for i in range(1, 9)])
                for argv in commands:
                    self.assertEqual(argv[-len(selectors) - 2 : -1], [*selectors, "--shard"])
        for value, workers in (("0/1", 4), ("2/1", 4), ("1/0", 4), ("1/1", 0)):
            with self.subTest(value=value, workers=workers), self.assertRaises(ValueError):
                runner.selection_parallel("Unit", value, workers, ["--unit"])

    def test_unit_shards_are_deterministic_complete_and_disjoint(self):
        cases = [unittest.FunctionTestCase(lambda: None, description=str(i)) for i in range(7)]
        for i, case in enumerate(cases):
            case.id = lambda i=i: f"example.Case.test_{i}"
        suites = [
            list(runner.tests(runner.shard(unittest.TestSuite(reversed(cases)), f"{i}/3")))
            for i in range(1, 4)
        ]
        ids = [test.id() for suite in suites for test in suite]
        self.assertEqual(sorted(ids), sorted(test.id() for test in cases))
        self.assertEqual([len(suite) for suite in suites], [3, 2, 2])
        self.assertEqual([test.id() for test in suites[0]], [cases[i].id() for i in (0, 3, 6)])

    def test_invalid_or_duplicate_unit_shards_fail(self):
        case = unittest.FunctionTestCase(lambda: None)
        for value in ("0/1", "2/1", "1/0", "1/2", "1", "1/1/1", "a/1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                runner.shard(unittest.TestSuite([case]), value)
        with self.assertRaises(ValueError):
            runner.shard(unittest.TestSuite([case, case]), "1/1")

    def selection_fixture(self, *, normalized=False):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        package = "example_" + root.name + "_tests"
        directory = root / (f"tests/{package}/integration" if normalized else ".github")
        directory.mkdir(parents=True)
        if normalized:
            self.enterContext(
                mock.patch.object(tests, "__path__", [*tests.__path__, str(root / "tests")])
            )
            for parent in (directory, directory.parent):
                (parent / "__init__.py").touch()
        name = "selection_" + root.name + "_test"
        path = directory / (name + ".py")
        path.write_text(
            "import unittest\nclass Choice(unittest.TestCase):\n"
            "    def test_one(self): pass\n    def test_two(self): pass\n"
        )
        module = ("tests." + package + ".integration." if normalized else "") + name
        return root, path, module

    def test_exact_id_and_file_select_only_the_requested_tests(self):
        for normalized in (False, True):
            with self.subTest(normalized=normalized):
                root, path, module = self.selection_fixture(normalized=normalized)
                test_id = module + ".Choice.test_one"
                suite, paths = runner.select(root, [test_id])
                self.assertEqual([test.id() for test in runner.tests(suite)], [test_id])
                self.assertEqual(paths, [path])
                suite, _ = runner.select(root, [str(path.relative_to(root))])
                self.assertEqual(suite.countTestCases(), 2)
                suite, _ = runner.select(root, [str(path.parent)])
                self.assertEqual(suite.countTestCases(), 2)

    def test_invalid_or_nonunique_inputs_never_fall_back(self):
        root, path, module = self.selection_fixture()
        (root / "ordinary.py").touch()
        for selector in (
            module + ".Choice.test_missing",
            module + ".Choice",
            "missing.py",
            "ordinary.py",
            "../outside_test.py",
            str(path) + ".missing",
        ):
            with self.subTest(selector=selector), self.assertRaises(ValueError):
                runner.select(root, [selector])
        original = list(runner.tests(runner.discover(root, [path])))[0]
        duplicate = unittest.TestSuite([original, original])
        with (
            mock.patch.object(runner, "discover", return_value=duplicate),
            self.assertRaises(ValueError),
        ):
            runner.select(root, [original.id()])

    def test_ambiguous_module_names_are_rejected_before_import(self):
        root, path, module = self.selection_fixture(normalized=True)
        other = root / ".github" / (module + ".py")
        other.parent.mkdir(parents=True)
        for parent in (other.parent, other.parent.parent):
            (parent / "__init__.py").touch()
        other.write_text(path.read_text())
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            runner.select(root, [module + ".Choice.test_one"])

    def test_inherited_state_is_not_a_supervised_child(self):
        with mock.patch.dict(os.environ, {runner.STATE: "inherited"}, clear=True):
            self.assertFalse(runner.supervised_child())
        with mock.patch.dict(
            os.environ,
            {runner.STATE: "state", "RUN_TESTS_PARENT": str(os.getppid())},
            clear=True,
        ):
            self.assertTrue(runner.supervised_child())

    def test_top_level_preserves_project_packages_and_normalized_tests(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        regular = root / "tests/session/unit"
        normalized = root / "tests/orchestrator/unit"
        for path in (regular, normalized):
            path.mkdir(parents=True)
            (path / "__init__.py").touch()
        (normalized.parent / "__init__.py").touch()
        self.assertEqual(runner.discovery_top(root, regular), root.resolve())
        self.assertEqual(runner.discovery_top(root, normalized), root.resolve())
        self.assertEqual(
            runner.discovery_top(root, (root / ".github").resolve()), (root / ".github").resolve()
        )
