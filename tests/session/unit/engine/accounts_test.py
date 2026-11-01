"""Per-account live usage readings and an account's headroom."""

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
