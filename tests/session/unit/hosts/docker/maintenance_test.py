"""hosts/docker/maintenance.py: the credential tick as a state machine, driven at chosen times with
a fake broker, and run's waits between ticks.
"""

import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace

from ensemble.session.engine.types import SessionError
from ensemble.session.hosts.docker import maintenance
from tests.session.case import Case

NOW = 1_792_238_400.0  # 2026-10-17T12:00:00Z
GEN = "0123456789abcdef0123456789abcdef"
REASON = "Client authentication expired; rerun the broker login on the host, then resume."


def at(seconds):
    return datetime.fromtimestamp(NOW + seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def deadline(seconds):
    return {"at": at(seconds), "action": "close", "reason": REASON}


def found(next_check=None, until=None, results=(), notices=()):
    return SimpleNamespace(
        next_check=next_check, deadline=until, results=list(results), notices=list(notices)
    )


class Broker:
    """Each maintain pops the next result or exception; close pops from `closes`."""

    def __init__(self, *results, closes=()):
        self.results, self.closes, self.calls = list(results), list(closes), []

    def maintain(self, container, manifest, delivered, *, root):
        self.calls.append(("maintain", container, manifest, list(delivered), root))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def close(self, container):
        self.calls.append(("close", container))
        if self.closes and isinstance(error := self.closes.pop(0), BaseException):
            raise error


class TickTest(Case):
    def setUp(self):
        self.state = os.path.join(self.home, "state")
        os.makedirs(self.state)
        self.reports, self.closed = [], 0

    def make(self, broker, *, close=None):
        def closing():
            self.closed += 1
            broker.close(f"ensemble-worker_1-{GEN}")

        return maintenance.Maintenance(
            "worker_1",
            self.state,
            GEN,
            broker,
            report=lambda word, text: self.reports.append((word, text)),
            root="/repo",
            close=close or closing,
            clock=lambda: NOW,
        )

    def cache(self, value):
        with open(maintenance.deadline_file(self.state, GEN), "w") as stream:
            json.dump(value, stream)

    def test_first_call_is_due_at_construction_then_next_check(self):
        broker = Broker(found(at(3000)))
        tick = self.make(broker)
        self.assertEqual(tick.due, NOW)
        self.assertEqual(tick.tick(NOW), NOW + 3000)
        self.assertEqual(tick.due, NOW + 3000)
        self.assertEqual(
            broker.calls,
            [
                (
                    "maintain",
                    f"ensemble-worker_1-{GEN}",
                    os.path.join(self.state, f"{GEN}.json"),
                    [],
                    "/repo",
                )
            ],
        )
        self.assertEqual(tick.tick(NOW + 10), NOW + 3000)
        self.assertEqual(len(broker.calls), 1)

    def test_failure_keeps_the_cached_deadline_and_retries_each_minute(self):
        self.cache(deadline(150))
        broker = Broker(SessionError("down"), SessionError("still down"), found(at(9000)))
        tick = self.make(broker)
        self.assertEqual(tick.tick(NOW), NOW + 60)
        self.assertEqual(tick.tick(NOW + 60), NOW + 120)
        self.assertEqual(tick.tick(NOW + 120), NOW + 150)
        self.assertEqual(self.reports, [("credential_maintenance_failed", "transport")])
        with open(maintenance.deadline_file(self.state, GEN)) as stream:
            self.assertEqual(json.load(stream), deadline(150))

    def test_expiry_reports_once_then_closes_retrying_each_minute(self):
        self.cache(deadline(100))
        broker = Broker(
            SessionError("down"), SessionError("down"), closes=[SessionError("busy"), None]
        )
        tick = self.make(broker)
        self.assertEqual(tick.tick(NOW), NOW + 60)
        self.assertEqual(tick.tick(NOW + 60), NOW + 100)
        self.assertEqual(tick.tick(NOW + 100), NOW + 160)
        self.assertIsNone(tick.tick(NOW + 160))
        self.assertIsNone(tick.due)
        self.assertEqual(self.closed, 2)
        self.assertEqual(
            self.reports,
            [("credential_maintenance_failed", "transport"), ("client_auth_expired", REASON)],
        )

    def test_a_passed_cached_deadline_still_ticks_first(self):
        self.cache(deadline(-100))
        broker = Broker(found(at(3600), deadline(7200)))
        self.assertEqual(self.make(broker).tick(NOW), NOW + 3600)
        self.assertEqual(self.reports, [])

    def test_notice_is_reported_then_acknowledged_at_once(self):
        notice = {"key": "env/db@1:7d", "text": "env/db expires in 6 days: rerun its login"}
        broker = Broker(found(at(3600), notices=[notice]), found(at(3600)))
        tick = self.make(broker)
        self.assertEqual(tick.tick(NOW), NOW)
        self.assertEqual(tick.tick(NOW), NOW + 3600)
        self.assertEqual(broker.calls[1][3], ["env/db@1:7d"])
        self.assertEqual(self.reports, [("credential_notice", notice["text"])])

    def test_a_deadline_without_next_check_is_still_enforced(self):
        broker = Broker(found(None, deadline(500)))
        tick = self.make(broker)
        self.assertEqual(tick.tick(NOW), NOW + 500)
        self.assertIsNone(tick.tick(NOW + 500))
        self.assertEqual(self.closed, 1)
        self.assertEqual(self.reports, [("client_auth_expired", REASON)])

    def test_no_deadline_and_no_next_check_is_done(self):
        tick = self.make(Broker(found()))
        self.assertIsNone(tick.tick(NOW))
        self.assertIsNone(tick.due)

    def test_reports_carry_only_broker_names_messages_and_notice_text(self):
        secret = "ghs_SENTINELs3cr3tTOKEN"
        result = {
            "credential": "github",
            "expires_at": None,
            "token": secret,
            "error": {"code": "refresh_failed", "message": "Renewal failed.", "cause": "503"},
            "value": secret,
        }
        notice = {"key": "k", "text": "renew soon", "secret": secret}
        broker = Broker(found(at(60), results=[result], notices=[notice]))
        self.make(broker).tick(NOW)
        self.assertEqual(
            self.reports,
            [
                ("credential_refresh_failed", "github: Renewal failed. (503)"),
                ("credential_notice", "renew soon"),
            ],
        )
        self.assertNotIn(secret, repr(self.reports))

    def test_run_waits_for_each_due_time_and_stops_when_asked(self):
        clock = [NOW]
        waits = []

        class Stopped:
            def wait(self, delay):
                waits.append(delay)
                clock[0] += delay
                return len(waits) > 2

        broker = Broker(found(at(30)), found(at(90)), found(at(500)))
        tick = maintenance.Maintenance(
            "worker_1",
            self.state,
            GEN,
            broker,
            report=lambda *_: None,
            root="/repo",
            close=lambda: None,
            clock=lambda: clock[0],
        )
        maintenance.run(tick, Stopped(), clock=lambda: clock[0])
        self.assertEqual(waits, [0.0, 30.0, 60.0])
        self.assertEqual(len(broker.calls), 2)
