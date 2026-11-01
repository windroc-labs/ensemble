"""Per-account live usage readings, an account's headroom, and the account a start picks."""

import io
import math
import os
import shutil
import tempfile
import threading
from contextlib import redirect_stderr
from unittest import mock

from ensemble.session.engine import accounts
from ensemble.session.engine.types import Launch, Usage, Window
from tests.session.case import Case


def usage(five, week=None):
    return Usage(
        None if five is None else Window(five, None), None if week is None else Window(week, None)
    )


class LeftTest(Case):
    def test_both_windows_share_one_limit(self):
        for five, week, left in (
            (80, 10, 0),
            (10, 80, 0),
            (85, 10, -5),
            (79, 79, 1),
            (None, 70, 10),
            (70, None, 10),
        ):
            with self.subTest(five=five, week=week):
                self.assertEqual(accounts.left(usage(five, week), 80), left)

    def test_no_window_is_unbounded_and_no_reading_unknown(self):
        self.assertEqual(accounts.left(Usage(None, None), 80), math.inf)
        self.assertIsNone(accounts.left(None, 80))


class LiveTest(Case):
    def test_every_account_is_read_once_concurrently_with_no_cache(self):
        barrier = threading.Barrier(2, timeout=5)
        launches = {"home": Launch(("p", "/h")), "work": Launch(("p", "/w"))}

        def read(launch):
            barrier.wait()
            return usage(10) if launch.argv[-1] == "/h" else None

        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home)
        with mock.patch.dict(os.environ, {"HOME": home}), redirect_stderr(io.StringIO()):
            self.assertEqual(accounts.live(launches, read), {"home": usage(10), "work": None})
        self.assertEqual(os.listdir(home), [])

    def test_no_account_reads_nothing(self):
        self.assertEqual(accounts.live({}, mock.Mock(side_effect=AssertionError)), {})


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

    def test_equal_readings_pick_by_name_alone(self):
        found = {"b": usage(30), "a": usage(10, 30), "default": usage(30, 10)}
        self.assertEqual((accounts.pick(found), accounts.best(found)), ("a", "a"))
        found = {"default": usage(95, 10), "a": usage(95, 10)}
        self.assertEqual((accounts.pick(found), accounts.best(found)), (None, "a"))
        self.assertEqual(accounts.pick({"b": usage(5), "a": usage(5)}), "a")
        over = {"b": usage(95), "a": usage(95), "c": usage(99)}
        self.assertEqual((accounts.pick(over), accounts.best(over)), (None, "a"))
