"""Broker gate tests use fake GitHub responses and time; no platform APIs."""

import contextlib
import io
import json
import os
import unittest
from unittest import mock

import ci_broker


def job(name, status="completed", conclusion="success", *, ran=True):
    return {
        "id": 1 if name == "Broker ubuntu-24.04" else 2,
        "run_id": 42,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "started_at": "2026-10-26T17:38:30Z",
        "runner_id": 7 if ran else 0,
        "runner_name": "runner" if ran else "",
        "steps": [{"status": "completed", "conclusion": "success"}] if ran else [],
    }


class BrokerGate(unittest.TestCase):
    def setUp(self):
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {
                    "GITHUB_TOKEN": "fake-token",
                    "GITHUB_API_URL": "https://api.github.com",
                    "GITHUB_REPOSITORY": "owner/repo",
                    "GITHUB_RUN_ID": "42",
                    "GITHUB_RUN_ATTEMPT": "3",
                },
            )
        )
        self.clock = self.enterContext(mock.patch("time.monotonic", side_effect=range(10000)))
        self.sleep = self.enterContext(mock.patch("time.sleep"))
        self.urlopen = self.enterContext(mock.patch("urllib.request.urlopen"))
        self.out = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def pages(self, *snapshots):
        responses = []
        for jobs in snapshots:
            data = {"total_count": len(jobs), "jobs": jobs} if isinstance(jobs, list) else jobs
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(data).encode()
            responses.append(response)
        self.urlopen.side_effect = responses

    def pair(self, mac=None, linux=None):
        return [
            job("Broker ubuntu-24.04") if linux is None else linux,
            job("Broker macos-15") if mac is None else mac,
        ]

    def test_success_and_current_attempt_endpoint(self):
        self.pages(self.pair())
        self.assertEqual(ci_broker.main(), 0)
        request = self.urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://api.github.com/repos/owner/repo/actions/runs/42/attempts/3/jobs?per_page=100&page=1",
        )
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-token")
        self.assertIsInstance(self.urlopen.call_args.kwargs["timeout"], int)

    def test_mac_failure_and_started_cancellation_block(self):
        for outcome in ("failure", "cancelled", "timed_out", "skipped", "neutral", None):
            with self.subTest(outcome=outcome):
                self.pages(self.pair(job("Broker macos-15", conclusion=outcome)))
                self.assertEqual(ci_broker.main(), 1)
                self.assertEqual(self.urlopen.call_args.args[0].get_method(), "GET")

    def test_unstarted_cancellation_passes(self):
        self.pages(self.pair(job("Broker macos-15", conclusion="cancelled", ran=False)))
        self.assertEqual(ci_broker.main(), 0)
        self.assertEqual(self.urlopen.call_count, 1)

    def test_queued_mac_is_cancelled_then_rechecked(self):
        self.pages(
            self.pair(job("Broker macos-15", "queued", None, ran=False)),
            {},
            self.pair(job("Broker macos-15", "completed", "cancelled", ran=False)),
        )
        self.assertEqual(ci_broker.main(), 0)
        requests = [call.args[0] for call in self.urlopen.call_args_list]
        self.assertEqual([request.get_method() for request in requests], ["GET", "POST", "GET"])
        self.assertEqual(
            requests[1].full_url,
            "https://api.github.com/repos/owner/repo/actions/runs/42/cancel",
        )
        self.assertIn("never started", self.out.getvalue())

    def test_start_during_cancel_race_blocks(self):
        self.pages(
            self.pair(job("Broker macos-15", "queued", None, ran=False)),
            {},
            self.pair(job("Broker macos-15", "completed", "cancelled", ran=True)),
        )
        self.assertEqual(ci_broker.main(), 1)

    def test_running_mac_waits_for_outcome_and_remembers_execution(self):
        for outcome, expected in (("success", 0), ("failure", 1), ("cancelled", 1)):
            with self.subTest(outcome=outcome):
                self.pages(
                    self.pair(job("Broker macos-15", "in_progress", None)),
                    self.pair(job("Broker macos-15", conclusion=outcome, ran=False)),
                )
                self.assertEqual(ci_broker.main(), expected)
                self.assertTrue(self.sleep.called)

    def test_linux_failure_blocks_even_with_queued_or_cancelled_mac(self):
        for status, outcome in (("queued", None), ("completed", "cancelled")):
            self.pages(
                self.pair(
                    job("Broker macos-15", status, outcome, ran=False),
                    job("Broker ubuntu-24.04", conclusion="failure"),
                )
            )
            self.assertEqual(ci_broker.main(), 1)
            self.assertEqual(self.urlopen.call_args.args[0].get_method(), "GET")

    def test_linux_pending_is_not_cancelled(self):
        self.pages(
            self.pair(
                job("Broker macos-15", "queued", None, ran=False),
                job("Broker ubuntu-24.04", "in_progress", None),
            ),
            self.pair(),
        )
        self.assertEqual(ci_broker.main(), 0)
        self.assertTrue(all(c.args[0].get_method() == "GET" for c in self.urlopen.call_args_list))

    def test_missing_duplicate_wrong_run_and_malformed_jobs_block(self):
        for jobs in (
            [],
            [job("Broker ubuntu-24.04")],
            [*self.pair(), job("Broker macos-15")],
            self.pair({**job("Broker macos-15"), "run_id": 99}),
            self.pair({**job("Broker macos-15"), "status": "unknown"}),
            {"jobs": self.pair()},
            {"total_count": 3, "jobs": self.pair()},
        ):
            with self.subTest(jobs=jobs):
                self.pages(jobs)
                self.assertEqual(ci_broker.main(), 1)

    def test_incomplete_execution_metadata_cannot_authorize_exception(self):
        for field in ("runner_id", "runner_name", "steps"):
            mac = job("Broker macos-15", conclusion="cancelled", ran=False)
            mac.pop(field)
            self.pages(self.pair(mac))
            self.assertEqual(ci_broker.main(), 1)
        for field, value in (
            ("runner_id", False),
            ("runner_id", 8),
            ("runner_name", "assigned"),
            ("steps", [{"status": "completed", "conclusion": "success"}]),
        ):
            mac = {**job("Broker macos-15", conclusion="cancelled", ran=False), field: value}
            self.pages(self.pair(mac))
            self.assertEqual(ci_broker.main(), 1)

    def test_pagination_does_not_miss_a_broker_leg(self):
        extras = [{**job(f"Other {i}"), "id": i + 10} for i in range(99)]
        self.pages(
            {"total_count": 101, "jobs": [*extras, job("Broker ubuntu-24.04")]},
            {"total_count": 101, "jobs": [job("Broker macos-15")]},
        )
        self.assertEqual(ci_broker.main(), 0)
        self.assertTrue(self.urlopen.call_args.args[0].full_url.endswith("page=2"))

    def test_polling_is_bounded_and_cancel_sent_once(self):
        pending = self.pair(job("Broker macos-15", "queued", None, ran=False))
        self.pages(pending, {}, pending, pending)
        self.clock.side_effect = [0, 0, 0, 1201]
        self.assertEqual(ci_broker.main(), 1)
        self.assertEqual(
            sum(c.args[0].get_method() == "POST" for c in self.urlopen.call_args_list), 1
        )

    def test_api_errors_and_invalid_environment_fail_closed(self):
        self.urlopen.side_effect = OSError("unavailable")
        self.assertEqual(ci_broker.main(), 1)
        for key in ("GITHUB_TOKEN", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_REPOSITORY"):
            with self.subTest(key=key), mock.patch.dict(os.environ, {key: ""}):
                self.assertEqual(ci_broker.main(), 1)
