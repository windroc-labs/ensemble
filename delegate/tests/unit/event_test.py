"""delegate/event/__init__.py: the reply protocol's names."""

import unittest

from ensemble.delegate import event as protocol

from delegate.tests.case import Case


class ProtocolTest(Case):
    def test_the_names(self):
        self.assertEqual(
            (protocol.PROGRESS, protocol.OUTCOME), ("ensemble-progress", "ensemble-outcome")
        )


if __name__ == "__main__":
    unittest.main()
