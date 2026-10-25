"""Usage thresholds every layer's quota check shares."""

from types import SimpleNamespace

from ensemble import utils
from ensemble.utils import exhausted
from ensemble.utils.quota import MAX_5H, MAX_WEEK
from tests.utils.case import Case


def window(used):
    return None if used is None else SimpleNamespace(used_percent=used)


def reading(five, week):
    return SimpleNamespace(five_hour=window(five), weekly=window(week))


class Exhausted(Case):
    def test_thresholds(self):
        self.assertEqual((MAX_5H, MAX_WEEK), (90, 100))
        self.assertFalse({"MAX_5H", "MAX_WEEK"} & set(utils.__all__))

    def test_either_window_at_its_threshold_is_exhausted(self):
        for five, week, want in (
            (89.9, 99.9, False),
            (90, 0, True),
            (0, 100, True),
            (None, None, False),
            (None, 100, True),
            (95, None, True),
        ):
            with self.subTest(five=five, week=week):
                self.assertIs(exhausted(reading(five, week)), want)

    def test_a_lower_5h_threshold(self):
        self.assertTrue(exhausted(reading(80, 0), 80))
        self.assertFalse(exhausted(reading(79, 0), 80))
