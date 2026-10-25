import os
import unittest

from tests import guards
from tests.case import ROOTS, Case


class Levels(Case):
    def test_the_levels_name_each_project_of_this_tree_once(self):
        named = [project for level in guards.LEVELS for project in level]
        self.assertEqual(
            sorted(named), sorted(os.path.basename(os.path.dirname(root)) for root in ROOTS)
        )

    def test_the_layer_order(self):
        self.assertEqual(guards.LAYERS, ("utils", "session", "delegate", "orchestrator", "manager"))


if __name__ == "__main__":
    unittest.main()
