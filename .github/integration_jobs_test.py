"""Duration scheduling and complete integration-shard coverage."""

import contextlib
import io
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import integration_jobs as gate


def case(name):
    return type(name, (unittest.TestCase,), {"runTest": lambda _self: None})()


class Assignment(unittest.TestCase):
    def test_shards_discover_only_project_integration_suites(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for directory in (
                "tests/integration",
                "tests/session/integration",
                "tests/orchestrator/integration",
            ):
                package = root / directory
                package.mkdir(parents=True)
                (package / "__init__.py").touch()
            self.assertEqual(
                gate.directories(root),
                [
                    "tests/orchestrator/integration",
                    "tests/session/integration",
                ],
            )

    def test_longest_tests_are_assigned_first_to_the_least_loaded_shard(self):
        found = [case(name) for name in ("Session", "Delegate", "Manager", "Orchestrator")]
        durations = dict(zip((test.id() for test in found), (10, 8, 2, 1), strict=True))
        split = gate.assign(found, ("1", "2"), durations)
        self.assertEqual([type(test).__name__ for test in split["1"]], ["Session", "Orchestrator"])
        self.assertEqual([type(test).__name__ for test in split["2"]], ["Delegate", "Manager"])
        gate.validate(found, split, ("2", "1"))

    def test_new_tests_use_the_median_and_ties_are_deterministic(self):
        found = [case(name) for name in ("A", "B", "C")]
        known = {found[0].id(): 2.0, found[1].id(): 6.0}
        self.assertEqual(gate.weights(found, known)[found[2].id()], 4.0)
        self.assertEqual(
            gate.assign(found, ("1", "2"), known),
            gate.assign(list(reversed(found)), ("1", "2"), known),
        )
        self.assertEqual(set(gate.weights(found, {}).values()), {1.0})

    def test_invalid_durations_fail(self):
        for duration in (-1, 0, float("inf"), float("nan"), True, "slow"):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                gate.weights([case("A")], {"unused": duration})

    def test_missing_duplicate_and_empty_shards_fail(self):
        a, b = case("A"), case("B")
        for suites in (
            {"1": [a], "2": [a, b]},
            {"1": [a], "2": []},
            {"1": [a], "2": [case("C")]},
            {"1": [a, b]},
        ):
            with self.subTest(suites=suites), self.assertRaises(ValueError):
                gate.validate([a, b], suites, ("1", "2"))


class Workflow(unittest.TestCase):
    def test_worker_shards_follow_the_actual_matrix_and_worker_count(self):
        text = 'jobs:\n  integration:\n    env:\n      INTEGRATION_WORKERS: "3"\n    strategy:\n      matrix:\n        job: ["1", "2"]\n'
        self.assertEqual(gate.workflow_shards(text), [str(i) for i in range(1, 7)])
        for value in ('"0"', '"many"', '"-1"'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                gate.workflow_shards(text.replace('"3"', value))

    def test_matrix_must_have_unique_string_labels(self):
        for matrix in ('["1", "2", "1"]', '["1", 2]', "[1, 2]", "[]", '["1", "3"]', '["2"]'):
            text = f"jobs:\n  integration:\n    strategy:\n      matrix:\n        job: {matrix}\n"
            with self.subTest(matrix=matrix), self.assertRaises(ValueError):
                gate.workflow_jobs(text)
        text = 'jobs:\n  integration:\n    strategy:\n      matrix:\n        job: ["1", "2"]\n'
        self.assertEqual(gate.workflow_jobs(text), ["1", "2"])


class Workers(unittest.TestCase):
    def test_every_worker_is_awaited_and_keeps_private_logs_and_timings(self):
        for timing in (False, True):
            with self.subTest(timing=timing), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                calls = []

                def run(argv, *, env, stdout, calls=calls, **_kwargs):
                    calls.append((argv[-1], env))
                    stdout.write(f"failure context {argv[-1]}\n")
                    return types.SimpleNamespace(returncode=int(argv[-1] == "1"))

                out = io.StringIO()
                with (
                    mock.patch.dict(os.environ),
                    mock.patch.object(gate.run_tests.subprocess, "run", side_effect=run),
                    contextlib.redirect_stdout(out),
                ):
                    os.environ.pop("INTEGRATION_TIMING_DIR", None)
                    if timing:
                        os.environ["INTEGRATION_TIMING_DIR"] = str(root)
                    self.assertEqual(gate.parallel("1", ["1", "2"]), 1)
                self.assertEqual(sorted(label for label, _ in calls), ["1", "2"])
                paths = [env["RUN_TESTS_DURATIONS"] for _, env in calls]
                self.assertEqual(len(set(paths)), 2)
                for label, env in calls:
                    self.assertIn(f"failure context {label}", out.getvalue())
                    if timing:
                        target = root / f"shard-{label}"
                        self.assertEqual(env["INTEGRATION_TIMING_DIR"], str(target))
                        self.assertTrue((target / "worker.log").is_file())
                    else:
                        self.assertFalse("INTEGRATION_TIMING_DIR" in env)

    def test_a_launch_error_fails_without_abandoning_other_workers(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        calls = []

        def run(argv, **_kwargs):
            calls.append(argv[-1])
            if argv[-1] == "1":
                raise OSError("cannot launch")
            return types.SimpleNamespace(returncode=0)

        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"INTEGRATION_TIMING_DIR": str(root)}),
            mock.patch.object(gate.run_tests.subprocess, "run", side_effect=run),
            contextlib.redirect_stdout(out),
        ):
            self.assertEqual(gate.parallel("1", ["1", "2"]), 1)
        self.assertEqual(sorted(calls), ["1", "2"])
        self.assertIn("before any test started", out.getvalue())
