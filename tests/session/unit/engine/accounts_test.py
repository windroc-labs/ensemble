"""Per-account usage readings, their cache, and the account a start picks."""

import io
import json
import os
import stat
import threading
from contextlib import redirect_stderr
from unittest import mock

from ensemble.session.engine import accounts
from ensemble.session.engine.types import Launch, Usage, Window
from tests.session.case import Case

NOW = 1_000_000.0


def usage(five, week=None):
    return Usage(
        None if five is None else Window(five, None), None if week is None else Window(week, None)
    )


class PickTest(Case):
    def test_the_account_with_more_left_wins(self):
        found = {"default": usage(70, 10), "work": usage(20, 30)}
        self.assertEqual((accounts.pick(found), accounts.best(found)), ("work", "work"))

    def test_an_account_over_a_threshold_loses_to_one_under(self):
        found = {"default": usage(92, 5), "work": usage(50, 60)}
        self.assertEqual(accounts.pick(found), "work")
        self.assertEqual(accounts.pick({"default": usage(10, 100), "work": usage(89, 1)}), "work")

    def test_with_every_account_over_best_is_the_one_with_most_left(self):
        found = {"default": usage(95, 10), "work": usage(10, 100), "lab": usage(91, 92)}
        self.assertIsNone(accounts.pick(found))
        self.assertEqual(accounts.best(found), "lab")

    def test_unknown_readings_are_never_picked(self):
        self.assertEqual(accounts.best({"default": None, "work": usage(95)}), "work")
        self.assertEqual(accounts.pick({"default": None, "work": usage(40)}), "work")
        self.assertIsNone(accounts.best({"default": None, "work": None}))
        self.assertIsNone(accounts.best({}))

    def test_a_reading_without_windows_has_everything_left(self):
        self.assertEqual(accounts.pick({"default": usage(1, 1), "work": usage(None)}), "work")

    def test_a_tie_goes_to_default_then_name_order(self):
        found = {"b": usage(30), "a": usage(10, 30), "default": usage(30, 10)}
        self.assertEqual((accounts.pick(found), accounts.best(found)), ("default", "default"))
        self.assertEqual(accounts.pick({"b": usage(5), "a": usage(5)}), "a")
        over = {"b": usage(95), "a": usage(95), "c": usage(99)}
        self.assertEqual((accounts.pick(over), accounts.best(over)), (None, "a"))


class ReadingsTest(Case):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.ensemble, "usage", "claude.json")
        self.read: list[Launch] = []
        self.answers = {"/d": usage(70, 10), "/w": usage(20, 30)}
        self.probes = {"default": ("/d", self.launch("/d")), "work": ("/w", self.launch("/w"))}

    @staticmethod
    def launch(identity):
        return Launch(("claude",), env={"DIR": identity})

    def reader(self, launch):
        self.read.append(launch)
        return self.answers[launch.env["DIR"]]

    def readings(self, now=NOW, probes=None):
        err = io.StringIO()
        with mock.patch("time.time", return_value=now), redirect_stderr(err):
            found = accounts.readings("claude", probes or self.probes, self.reader)
        self.err = err.getvalue()
        return found

    def test_each_account_is_read_once_and_cached(self):
        self.assertEqual(self.readings(), {"default": usage(70, 10), "work": usage(20, 30)})
        self.assertEqual(sorted(launch.env["DIR"] for launch in self.read), ["/d", "/w"])
        self.assertEqual(
            sorted(self.err.splitlines()),
            [
                "claude usage: account default: five_hour=70% weekly=10%",
                "claude usage: account work: five_hour=20% weekly=30%",
            ],
        )
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.path)).st_mode), 0o700)

    def test_within_the_ttl_nothing_is_read_again(self):
        first = self.readings()
        self.read.clear()
        self.assertEqual(self.readings(NOW + accounts.TTL - 1), first)
        self.assertEqual((self.read, self.err), ([], ""))

    def test_at_the_ttl_or_from_the_future_it_is_read_again(self):
        self.readings()
        for now in (NOW + accounts.TTL, NOW - 1):
            with self.subTest(now=now):
                self.read.clear()
                self.readings(now)
                self.assertEqual(len(self.read), 2)
                self.readings()

    def test_a_failed_reading_is_cached_too(self):
        self.answers["/w"] = None
        self.assertIsNone(self.readings()["work"])
        self.assertIn("claude usage: account work: unknown", self.err)
        self.read.clear()
        self.assertIsNone(self.readings(NOW + 1)["work"])
        self.assertEqual(self.read, [])

    def test_a_failed_reading_is_retried_after_the_unknown_ttl(self):
        self.answers["/w"] = None
        self.readings()
        self.read.clear()
        self.readings(NOW + accounts.UNKNOWN_TTL - 1)
        self.assertEqual(self.read, [])
        self.answers["/w"] = usage(20, 30)
        self.assertEqual(self.readings(NOW + accounts.UNKNOWN_TTL)["work"], usage(20, 30))
        self.assertEqual([launch.env["DIR"] for launch in self.read], ["/w"])

    def test_another_identity_misses(self):
        self.readings()
        self.read.clear()
        self.answers["/w2"] = usage(1, 1)
        probes = {**self.probes, "work": ("/w2", self.launch("/w2"))}
        self.assertEqual(self.readings(NOW + 1, probes)["work"], usage(1, 1))
        self.assertEqual([launch.env["DIR"] for launch in self.read], ["/w2"])

    def test_accounts_reading_one_identity_share_one_read(self):
        probes = {**self.probes, "same": ("/d", self.launch("/d"))}
        found = self.readings(probes=probes)
        self.assertEqual(found["same"], found["default"])
        self.assertEqual(len(self.read), 2)

    def test_misses_are_read_concurrently(self):
        both = threading.Barrier(2, timeout=10)

        def reader(launch):
            both.wait()
            return self.answers[launch.env["DIR"]]

        with redirect_stderr(io.StringIO()):
            found = accounts.readings("claude", self.probes, reader)
        self.assertEqual(found["work"], usage(20, 30))

    def test_a_bad_cache_reads_as_empty(self):
        good = self.readings()
        entry = {"at": NOW, "identity": "/w", "usage": None}
        for name, text in (
            ("not json", "{"),
            ("not a table", "[]"),
            ("oversized", json.dumps({"work": entry, "pad": "x" * accounts.CACHE_MAX})),
            ("bad at", json.dumps({"work": {**entry, "at": True}})),
            ("bad identity", json.dumps({"work": {**entry, "identity": 1}})),
            ("bad usage", json.dumps({"work": {**entry, "usage": {"five_hour": [1]}}})),
            ("no usage", json.dumps({"work": {"at": NOW, "identity": "/w"}})),
            ("not finite", '{"work": {"at": NaN, "identity": "/w", "usage": null}}'),
        ):
            with self.subTest(name):
                with open(self.path, "w", encoding="utf-8") as f:
                    f.write(text)
                self.read.clear()
                self.assertEqual(self.readings(NOW + 1), good)
                self.assertEqual(len(self.read), 2)

    def test_a_fifo_never_blocks(self):
        os.makedirs(os.path.dirname(self.path))
        os.mkfifo(self.path)
        self.readings()
        self.assertEqual(len(self.read), 2)

    def test_a_cache_it_cannot_write_is_no_error(self):
        os.makedirs(os.path.dirname(self.path), mode=0o500)
        self.assertEqual(self.readings()["work"], usage(20, 30))
        self.assertFalse(os.path.exists(self.path))

    def test_the_cache_keeps_resets_and_drops_unregistered_accounts(self):
        self.answers["/w"] = Usage(Window(20, 1234.5), None)
        self.readings()
        self.read.clear()
        self.assertEqual(self.readings(NOW + 1)["work"], Usage(Window(20, 1234.5), None))
        self.assertEqual(self.read, [])
        self.readings(NOW + accounts.TTL, {"work": self.probes["work"]})
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(list(json.load(f)), ["work"])
