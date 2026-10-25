"""Utils public exports."""

import unittest

from ensemble import utils

from utils.tests.case import Case


class Exports(Case):
    def test_all_names_resolve_sorted_without_duplicates(self):
        self.assertEqual(utils.__all__, sorted(set(utils.__all__)))
        for name in utils.__all__:
            with self.subTest(name=name):
                self.assertTrue(hasattr(utils, name))


if __name__ == "__main__":
    unittest.main()
