"""Complete, successful timing samples are required before regeneration."""

import copy
import unittest

import integration_timings as timing


def sample(shard, tests, seconds, attempt=1):
    return (
        {
            "run_id": "123",
            "attempt": attempt,
            "sha": "merge-sha",
            "head_sha": "head-sha",
            "runner": "ubuntu-24.04",
            "architecture": "x86_64",
            "python": "3.11.1",
            "tmux": "tmux 3.4",
            "shards": ["1", "2"],
            "shard": shard,
            "discovered": ["a", "b"],
            "tests": tests,
            "exit_code": 0,
            "workflow": {
                "id": 123,
                "run_attempt": attempt,
                "head_sha": "head-sha",
                "status": "completed",
                "conclusion": "success",
                "event": "pull_request",
                "html_url": "https://github.com/acme/ensemble/actions/runs/123",
            },
        },
        [{"test": test, "seconds": value} for test, value in zip(tests, seconds, strict=True)],
    )


class Samples(unittest.TestCase):
    def setUp(self):
        self.samples = [sample("1", ["a"], [2]), sample("2", ["b"], [4])]

    def test_complete_attempts_aggregate_by_test_median(self):
        samples = self.samples + [sample("1", ["a"], [4], 2), sample("2", ["b"], [8], 2)]
        durations, sources = timing.aggregate(samples, {"a", "b"})
        self.assertEqual(durations, {"a": 3, "b": 6})
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0]["shards"], ["1", "2"])
        samples[2][0]["sha"] = samples[3][0]["sha"] = "different-commit"
        with self.assertRaises(ValueError):
            timing.aggregate(samples, {"a", "b"})

    def test_incomplete_failed_duplicate_or_mismatched_samples_are_rejected(self):
        variants = [self.samples[:1], self.samples + self.samples, []]
        for key, value in (
            ("exit_code", 1),
            ("runner", "macos-15"),
            ("architecture", "arm64"),
            ("discovered", ["a"]),
            ("tests", ["b"]),
            ("sha", "other"),
            ("matrix_jobs", ["different"]),
            ("cpus", 8),
        ):
            rows = copy.deepcopy(self.samples)
            rows[0][0][key] = value
            variants.append(rows)
        for rows in variants:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                timing.aggregate(rows, {"a", "b"})
        for key, value in (
            ("conclusion", "failure"),
            ("run_attempt", 9),
            ("head_sha", "other"),
            ("status", "in_progress"),
            ("id", 999),
        ):
            rows = copy.deepcopy(self.samples)
            rows[0][0]["workflow"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                timing.aggregate(rows, {"a", "b"})

    def test_every_test_requires_one_finite_positive_measurement(self):
        for values in ([], [0], [-1], [float("nan")], [float("inf")], [True], ["2"], [2, 3]):
            rows = copy.deepcopy(self.samples)
            rows[0] = (rows[0][0], [{"test": "a", "seconds": value} for value in values])
            with self.subTest(values=values), self.assertRaises(ValueError):
                timing.aggregate(rows, {"a", "b"})
        tiny = [sample("1", ["a"], [1e-9]), sample("2", ["b"], [4])]
        self.assertGreater(timing.aggregate(tiny, {"a", "b"})[0]["a"], 0)

    def test_discovery_changes_between_attempts_are_rejected(self):
        with self.assertRaises(ValueError):
            timing.aggregate(self.samples, {"a", "b", "c"})
