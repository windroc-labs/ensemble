"""A fake monotonic clock: calling it reads `now`, which only sleep (or a test) moves."""

from unittest import mock


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def patch(self, case):
        """Make time.monotonic read this clock and time.sleep advance it for `case`."""
        case.enterContext(mock.patch("time.monotonic", side_effect=self))
        case.enterContext(mock.patch("time.sleep", side_effect=self.sleep))
        return self
