"""A usage reading's one-line summary; no layer shares a usage threshold."""

from types import SimpleNamespace

from ensemble import utils
from ensemble.utils import quota, summary
from tests.utils.case import Case


def window(used):
    return None if used is None else SimpleNamespace(used_percent=used)


class Summary(Case):
    def test_each_window_or_none_and_no_reading_unknown(self):
        reading = SimpleNamespace(five_hour=window(20.5), weekly=window(None))
        self.assertEqual(summary(reading), "five_hour=20.5% weekly=none")
        self.assertEqual(summary(None), "unknown")

    def test_no_shared_usage_threshold(self):
        for name in ("exhausted", "MAX_5H", "MAX_WEEK"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(utils, name) or hasattr(quota, name))
