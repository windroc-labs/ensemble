"""tests/utils/clock.py: FakeClock, a monotonic clock that only sleep moves."""

import time

from tests.utils.case import Case
from tests.utils.clock import FakeClock


class FakeClockTest(Case):
    def test_now_is_its_reading_and_sleep_moves_it(self):
        clock = FakeClock(1000.0)
        self.assertEqual(clock(), 1000.0)
        clock.sleep(2.5)
        self.assertEqual((clock(), clock.now), (1002.5, 1002.5))
        clock.now = 7.0
        self.assertEqual(clock(), 7.0)
        self.assertEqual(FakeClock()(), 0.0)

    def test_patch_drives_time_monotonic_and_time_sleep(self):
        clock = FakeClock().patch(self)
        self.assertIsInstance(clock, FakeClock)
        time.sleep(3)
        self.assertEqual((time.monotonic(), clock.now), (3.0, 3.0))
        clock.now = 0.5
        self.assertEqual(time.monotonic(), 0.5)
