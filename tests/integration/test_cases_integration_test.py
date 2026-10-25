"""Every discovered test uses its project's Case, including Integration suites."""

import unittest

from tests import guards
from tests.case import REPO, Case
from tests.guard_samples import flat, own_case, test_suites


class EveryTest(Case):
    def test_is_a_case(self):
        loader, found = unittest.TestLoader(), []
        for top in guards.TESTS:
            for start, import_root in test_suites(REPO, top):
                tests = loader.discover(start, "*_test.py", import_root)
                found += [test.id() for test in flat(tests) if not own_case(test, top)]
        self.assertEqual((loader.errors, found), ([], []))
